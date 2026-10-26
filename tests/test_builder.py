"""Visualization builder: less common analysis/option combinations, built directly from plans."""

import pytest
from fixtures.factory import corpus

from app.analytics.citations import CitationFactory
from app.audit import audit
from app.ctgov import extract as ex
from app.schemas.plan import Filters, Group, QueryPlan
from app.viz.builder import Dataset, build


def run(plan: QueryPlan, groups: dict | None = None, mode="summary"):
    studies = corpus()
    if groups:
        datasets = [Dataset(label, Filters(), [s for s in studies if pred(s)], None) for label, pred in groups.items()]
    else:
        datasets = [Dataset(None, plan.filters, studies, len(studies))]
    by_id = {ex.nct_id(s): s for s in studies}
    res = build(plan, datasets, CitationFactory(by_id, {}, mode, 3))
    body = {"visualization": res.visualization.model_dump(mode="json"), "meta": {"citations": {"mode": mode}}}
    assert audit(body, by_id).ok
    return res


def P(**kw):
    return QueryPlan(interpretation="t", **kw)


def is_industry(s):
    return ex.get_path(s, f"{ex.P_LEAD}.class") == "INDUSTRY"


GROUPS = {"Industry": is_industry, "Non-industry": lambda s: not is_industry(s)}
GROUP_PLAN = [Group(label="Industry", filters=Filters(term="a")), Group(label="Non-industry", filters=Filters(term="b"))]


def test_time_trend_split_by_series_dimension():
    res = run(P(analysis="time_trend", time_field="start_date", time_granularity="year", series_dimension="phase"))
    rows = res.visualization.data
    assert res.visualization.encoding["color"].field == "group"
    assert {r["group"] for r in rows} >= {"Phase 3", "Phase 2"}
    years = sorted({r["period"] for r in rows})
    assert years == [str(y) for y in range(2015, 2021)]
    assert all(sum(1 for r in rows if r["period"] == y) == len({r["group"] for r in rows}) for y in years)


def test_quarterly_series_and_partial_flags():
    res = run(P(analysis="time_trend", time_field="start_date", time_granularity="quarter"))
    rows = res.visualization.data
    assert rows[0]["period"] == "2015-Q2" and rows[0]["period_start"] == "2015-04-01"
    assert res.visualization.encoding["x"].time_unit == "quarter"
    # NCT90000006 has a year-only start date, NCT90000008 none: neither can be placed in a quarter
    assert any(w.startswith("2 trial(s) had no usable start date at quarter precision") for w in res.warnings)
    assert all(r["is_projected"] is False for r in rows)


def test_completion_date_trend_flags_projected_periods():
    res = run(P(analysis="time_trend", time_field="primary_completion_date", time_granularity="year"))
    projected = [r for r in res.visualization.data if r["is_projected"]]
    assert projected and projected[-1]["period"] == "2027"
    assert any("anticipated" in w for w in res.warnings)


def test_percent_distribution_with_groups():
    res = run(P(analysis="distribution", dimension="phase", groups=GROUP_PLAN, metric="percent"), GROUPS)
    rows = res.visualization.data
    assert res.visualization.type == "grouped_bar_chart"
    for g in GROUPS:
        assert sum(r["percent"] for r in rows if r["group"] == g) == pytest.approx(100, abs=0.2)
        assert {r["group_total"] for r in rows if r["group"] == g} == {sum(1 for s in corpus() if GROUPS[g](s))}


def test_stacked_preference_for_groups():
    res = run(P(analysis="distribution", dimension="phase", groups=GROUP_PLAN, chart_preference="stacked"), GROUPS)
    assert res.visualization.type == "stacked_bar_chart" and res.rendering.stacked


def test_long_labels_render_horizontal():
    res = run(P(analysis="distribution", dimension="condition"))
    enc = res.visualization.encoding
    assert res.rendering.orientation == "horizontal"
    assert enc["y"].field == "condition" and enc["x"].field == "trial_count"


def test_heatmap_cross_tab_with_percent():
    res = run(P(analysis="cross_tab", dimension="phase", series_dimension="country", chart_preference="heatmap", metric="percent"))
    assert res.visualization.type == "heatmap"
    rows = res.visualization.data
    x_count = len(res.visualization.encoding["x"].sort)
    y_count = len(res.visualization.encoding["y"].sort)
    assert len(rows) == x_count * y_count  # dense grid incl. zero cells


def test_histogram_with_groups_shares_bins():
    res = run(P(analysis="histogram", numeric_field="enrollment", groups=GROUP_PLAN), GROUPS)
    rows = res.visualization.data
    starts = {g: [r["bin_start"] for r in rows if r["group"] == g] for g in GROUPS}
    assert starts["Industry"] == starts["Non-industry"]
    assert res.visualization.encoding["color"].field == "group"


def test_scatter_with_temporal_axis_and_groups():
    res = run(P(analysis="scatter", x_field="start_date", y_field="enrollment", groups=GROUP_PLAN), GROUPS)
    enc = res.visualization.encoding
    assert enc["x"].type == "temporal" and enc["y"].type == "quantitative"
    assert all(r["citation_count"] == 1 for r in res.visualization.data)


def test_trial_list_sorted_by_start_date():
    res = run(P(analysis="trial_list", numeric_field="start_date", top_n=3))
    rows = res.visualization.data
    assert [r["start_date"] for r in rows] == ["2020-03", "2019", "2018-07"]
    assert [r["rank"] for r in rows] == [1, 2, 3]


def test_network_with_groups_merges_and_warns():
    res = run(P(analysis="network", network_source="drug", network_target="drug", groups=GROUP_PLAN), GROUPS)
    assert any("merged" in w for w in res.warnings)
    ids = {n["id"] for n in res.visualization.data.nodes}
    assert all(e["source"] in ids and e["target"] in ids for e in res.visualization.data.edges)


def test_full_citation_mode_cites_every_contributor():
    res = run(P(analysis="distribution", dimension="country"), mode="full")
    for r in res.visualization.data:
        assert len(r["citations"]) == r["citation_count"] == r["trial_count"]
