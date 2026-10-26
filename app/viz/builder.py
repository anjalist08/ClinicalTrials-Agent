"""Build the visualization specification from a validated plan and fetched datasets.

One builder per analysis type; all share the same helpers for titles, citations,
percentages and category ordering, so adding a new question class means adding
one builder function rather than a one-off pipeline.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..analytics import aggregate as agg
from ..analytics.citations import CitationFactory
from ..ctgov import extract as ex
from ..schemas.enums import PHASE_LABELS, enum_label
from ..schemas.plan import Filters, QueryPlan
from ..schemas.response import Channel, NetworkData, Rendering, Visualization
from .geo import iso_codes

COUNT = "trial_count"
PERCENT = "percent"
GROUP = "group"


@dataclass
class Dataset:
    label: str | None  # comparison group label (None when there is no comparison)
    filters: Filters
    studies: list[dict[str, Any]]
    total_count: int | None


@dataclass
class BuildResult:
    visualization: Visualization
    rendering: Rendering
    counting_rule: str
    rationale: str
    warnings: list[str] = field(default_factory=list)
    assumptions: list[str] = field(default_factory=list)


# ---------------------------------------------------------------------- helpers
def describe_filters(f: Filters) -> str:
    parts: list[str] = []
    for v in (f.intervention, f.condition, f.term, f.sponsor):
        if v:
            parts.append(v)
    if f.country:
        parts.append(f"sites in {f.country}")
    if f.phases:
        parts.append(", ".join(PHASE_LABELS[p] for p in f.phases))
    if f.statuses:
        parts.append(", ".join(enum_label(s).lower() for s in f.statuses))
    if f.study_type:
        parts.append(enum_label(f.study_type).lower())
    if f.intervention_types:
        parts.append(", ".join(enum_label(t).lower() for t in f.intervention_types) + " interventions")
    if f.sponsor_classes:
        parts.append(", ".join(enum_label(c).lower() for c in f.sponsor_classes) + " sponsors")
    if f.start_year_from and f.start_year_to:
        parts.append(f"started {f.start_year_from}-{f.start_year_to}")
    elif f.start_year_from:
        parts.append(f"started {f.start_year_from} or later")
    elif f.start_year_to:
        parts.append(f"started by {f.start_year_to}")
    return " · ".join(parts)


def title_with(core: str, plan: QueryPlan, datasets: list[Dataset]) -> tuple[str, str | None]:
    if plan.groups:
        core = f"{core}: " + " vs ".join(d.label or "" for d in datasets)
    desc = describe_filters(plan.filters)
    return core, (desc or None)


def dim_title(dim: str) -> str:
    return ex.DIMENSIONS[dim].title


def dim_type(dim: str) -> str:
    return "ordinal" if ex.DIMENSIONS[dim].ordinal else "nominal"


def value_channel(plan: QueryPlan, axis_title: str | None = None) -> Channel:
    if plan.metric == "percent":
        return Channel(field=PERCENT, type="quantitative", title=axis_title or "% of trials in group", format=".1f", unit="%")
    return Channel(field=COUNT, type="quantitative", title=axis_title or "Number of trials", format="d", unit="trials")


def pct(n: int, d: int) -> float:
    return round(100.0 * n / d, 1) if d else 0.0


def group_channel(plan: QueryPlan) -> Channel:
    return Channel(field=GROUP, type="nominal", title="Comparison group", sort=[g.label for g in plan.groups])


def counting_rule_for(dim: str | None) -> str:
    base = "Each trial is counted at most once per category."
    if dim and ex.DIMENSIONS[dim].multi_valued:
        base += (
            f" '{dim_title(dim)}' is multi-valued (a trial can list several), so a trial may appear in "
            "several categories and category totals can exceed the number of trials."
        )
    return base


# ---------------------------------------------------------------------- builders
def build(plan: QueryPlan, datasets: list[Dataset], cites: CitationFactory) -> BuildResult:
    builder: Callable[..., BuildResult] = {
        "total": _total,
        "distribution": _distribution,
        "cross_tab": _cross_tab,
        "time_trend": _time_trend,
        "histogram": _histogram,
        "scatter": _scatter,
        "network": _network,
        "trial_list": _trial_list,
    }[plan.analysis]
    return builder(plan, datasets, cites)


def _all_bucket(studies: list[dict[str, Any]], key: str) -> agg.Bucket:
    b = agg.Bucket(key)
    for s in studies:
        nct = ex.nct_id(s)
        b.add(nct, key, [ex.Ev(ex.P_NCT, nct)])
    return b


def _total(plan: QueryPlan, datasets: list[Dataset], cites: CitationFactory) -> BuildResult:
    rows = []
    for d in datasets:
        value = d.total_count if d.total_count is not None else len(d.studies)
        row = {"label": d.label or "Matching trials", COUNT: value, "records_analyzed": len(d.studies)}
        row.update(cites.for_bucket(_all_bucket(d.studies, row["label"])))
        rows.append(row)
    rule = (
        "trial_count is the API's countTotal for the query (exact, independent of max_records); "
        "citations sample the retrieved records."
    )
    if len(datasets) > 1:
        title, sub = title_with("Number of matching trials", plan, datasets)
        enc = {"x": Channel(field="label", type="nominal", title="Group", sort="data"), "y": value_channel(plan)}
        return BuildResult(
            Visualization(type="bar_chart", title=title, subtitle=sub, encoding=enc, data=rows),
            Rendering(orientation="vertical", sort="group order", units={COUNT: "trials"}, value_field=COUNT, grouping="comparison group"),
            rule,
            "A count per comparison group is best compared side by side as bars.",
        )
    title, sub = title_with("Number of matching trials", plan, datasets)
    enc = {"label": Channel(field="label", type="nominal", title="Label"), "value": Channel(field=COUNT, type="quantitative", title="Trials", format=",d", unit="trials")}
    return BuildResult(
        Visualization(type="kpi", title=title, subtitle=sub, encoding=enc, data=rows),
        Rendering(units={COUNT: "trials"}, value_field=COUNT),
        rule,
        "The question asks for a single quantity, so a KPI card (single number) answers it; a chart would add nothing.",
    )


def _category_rows(
    plan: QueryPlan, datasets: list[Dataset], cites: CitationFactory, dim: str
) -> tuple[list[dict[str, Any]], list[str], int]:
    """Rows for (optionally grouped) category counts, with shared category order across groups."""
    per_group = {d.label: agg.categorical(d.studies, dim) for d in datasets}
    combined: dict[str, agg.Bucket] = {}
    for buckets in per_group.values():
        for k, b in buckets.items():
            cb = combined.setdefault(k, agg.Bucket(k))
            for nct, evs in b.evidence.items():
                cb.add(nct, b.label, evs)
    top_n = None if plan.chart_preference == "map" else plan.top_n
    keys = agg.order_keys(combined, dim, top_n)
    rows = []
    for d in datasets:
        buckets = per_group[d.label]
        denom = len(d.studies)
        for k in keys:
            b = buckets.get(k)
            n = b.count if b else 0
            row: dict[str, Any] = {dim: combined[k].label, COUNT: n}
            if plan.metric == "percent":
                row[PERCENT] = pct(n, denom)
                row["group_total"] = denom
            if d.label is not None:
                row[GROUP] = d.label
            row.update(cites.for_bucket(b))
            rows.append(row)
    return rows, [combined[k].label for k in keys], len(combined)


def _distribution(plan: QueryPlan, datasets: list[Dataset], cites: CitationFactory) -> BuildResult:
    dim = plan.dimension or "phase"
    rows, order, total_cats = _category_rows(plan, datasets, cites, dim)
    grouped = bool(plan.groups)
    nominal = dim_type(dim) == "nominal"
    rendering = Rendering(
        units={COUNT: "trials", PERCENT: "%"} if plan.metric == "percent" else {COUNT: "trials"},
        value_field=PERCENT if plan.metric == "percent" else COUNT,
        sort="natural category order" if not nominal else "descending by trial count (across all groups)",
        categories_total=total_cats,
        categories_shown=len(order),
        grouping="comparison group" if grouped else None,
    )
    core = f"Trials by {dim_title(dim).lower()}"

    if dim == "country" and plan.chart_preference == "map" and not grouped:
        for r in rows:
            a3, num = iso_codes(r["country"])
            r["iso_alpha3"], r["iso_numeric"] = a3, num
        title, sub = title_with("Trial locations by country", plan, datasets)
        enc = {
            "geo": Channel(field="iso_numeric", type="nominal", title="ISO 3166-1 numeric (world-atlas feature id)"),
            "label": Channel(field="country", type="nominal", title="Country"),
            "color": value_channel(plan, "Trials with ≥1 site"),
        }
        return BuildResult(
            Visualization(type="choropleth_map", title=title, subtitle=sub, encoding=enc, data=rows),
            rendering,
            counting_rule_for(dim) + " A trial counts for a country if at least one of its sites is there.",
            "Geographic question with a map preference: a choropleth shades each country by trial count.",
        )

    horizontal = plan.chart_preference == "horizontal_bar" or (
        nominal and (len(order) > 8 or max((len(o) for o in order), default=0) > 14)
    )
    cat = Channel(field=dim, type=dim_type(dim), title=dim_title(dim), sort=order)
    val = value_channel(plan)
    enc: dict[str, Channel] = {"y": cat, "x": val} if horizontal else {"x": cat, "y": val}
    rendering.orientation = "horizontal" if horizontal else "vertical"
    if grouped:
        enc["color"] = group_channel(plan)
        stacked = plan.chart_preference == "stacked"
        rendering.stacked = stacked
        vtype = "stacked_bar_chart" if stacked else "grouped_bar_chart"
        why = "Comparing a categorical distribution across groups: bars per category, one colour per group."
        if plan.metric == "percent":
            why += " Percentages normalise for different group sizes."
    else:
        vtype = "bar_chart"
        why = f"Counting trials per {dim_title(dim).lower()} category is a categorical distribution, best read as bars."
    title, sub = title_with(core, plan, datasets)
    return BuildResult(
        Visualization(type=vtype, title=title, subtitle=sub, encoding=enc, data=rows),
        rendering,
        counting_rule_for(dim),
        why,
    )


def _cross_tab(plan: QueryPlan, datasets: list[Dataset], cites: CitationFactory) -> BuildResult:
    dx, dy = plan.dimension or "phase", plan.series_dimension or "overall_status"
    studies = [s for d in datasets for s in d.studies]
    cells = agg.cross(studies, dx, dy)
    bx, by = agg.categorical(studies, dx), agg.categorical(studies, dy)
    heatmap = plan.chart_preference == "heatmap" or len(by) > 8
    kx = agg.order_keys(bx, dx, plan.top_n)
    ky = agg.order_keys(by, dy, plan.top_n if heatmap else 8)
    rows = []
    for x in kx:
        for y in ky:
            cell = cells.get((x, y))
            n = cell.count if cell else 0
            row = {dx: bx[x].label, dy: by[y].label, COUNT: n}
            if plan.metric == "percent":
                row[PERCENT] = pct(n, bx[x].count)
            row.update(cites.for_bucket(cell))
            rows.append(row)
    xs = Channel(field=dx, type=dim_type(dx), title=dim_title(dx), sort=[bx[k].label for k in kx])
    ys = Channel(field=dy, type=dim_type(dy), title=dim_title(dy), sort=[by[k].label for k in ky])
    rendering = Rendering(
        units={COUNT: "trials"},
        value_field=PERCENT if plan.metric == "percent" else COUNT,
        categories_total=len(bx),
        categories_shown=len(kx),
        grouping=dim_title(dy),
        sort="rows ordered by x category then series category",
    )
    title, sub = title_with(f"Trials by {dim_title(dx).lower()} and {dim_title(dy).lower()}", plan, datasets)
    rule = counting_rule_for(dx) + " " + counting_rule_for(dy).replace("Each trial is counted at most once per category. ", "")
    if plan.metric == "percent":
        rule += f" percent = share of trials within each {dim_title(dx).lower()} category."
    if heatmap:
        enc = {"x": xs, "y": ys, "color": value_channel(plan)}
        return BuildResult(
            Visualization(type="heatmap", title=title, subtitle=sub, encoding=enc, data=rows),
            rendering,
            rule,
            "Two categorical dimensions with many combinations: a heatmap shows every cell without clutter.",
        )
    rendering.stacked = True
    rendering.orientation = "vertical"
    enc = {"x": xs, "y": value_channel(plan), "color": ys}
    return BuildResult(
        Visualization(type="stacked_bar_chart", title=title, subtitle=sub, encoding=enc, data=rows),
        rendering,
        rule,
        "Breaking one categorical dimension down by another: stacked bars show totals and composition together.",
    )


_PERIOD_START = {
    "year": lambda k: f"{k}-01-01",
    "quarter": lambda k: f"{k[:4]}-{(int(k[-1]) - 1) * 3 + 1:02d}-01",
    "month": lambda k: f"{k}-01",
}
_TIME_TITLES = {
    "start_date": "start date",
    "primary_completion_date": "primary completion date",
    "completion_date": "completion date",
    "first_posted_date": "first posted date",
}


def _period_flags(start_iso: str, granularity: str, today: date) -> dict[str, bool]:
    start = date.fromisoformat(start_iso)
    if granularity == "year":
        end = date(start.year + 1, 1, 1)
    elif granularity == "quarter":
        m = start.month + 3
        end = date(start.year + (m > 12), (m - 1) % 12 + 1, 1)
    else:
        end = date(start.year + (start.month == 12), start.month % 12 + 1, 1)
    return {"is_partial": start <= today < end, "is_projected": start > today}


def _time_trend(plan: QueryPlan, datasets: list[Dataset], cites: CitationFactory) -> BuildResult:
    tf = plan.time_field or "start_date"
    gran = plan.time_granularity or "year"
    today = date.today()
    warnings: list[str] = []

    # series: comparison groups, or a split by a categorical dimension
    series: list[tuple[str | None, list[dict[str, Any]]]] = []
    split_dim = plan.series_dimension if not plan.groups else None
    if split_dim:
        studies = [s for d in datasets for s in d.studies]
        by_val = agg.categorical(studies, split_dim)
        keys = agg.order_keys(by_val, split_dim, min(plan.top_n or 6, 8))
        index = {ex.nct_id(s): s for s in studies}
        for k in keys:
            series.append((by_val[k].label, [index[n] for n in by_val[k].nct_ids]))
        if len(by_val) > len(keys):
            warnings.append(f"Showing the top {len(keys)} of {len(by_val)} {dim_title(split_dim).lower()} series.")
    else:
        series = [(d.label, d.studies) for d in datasets]

    per_series: list[tuple[str | None, dict[str, agg.Bucket], int]] = []
    skipped_total = 0
    for label, studies in series:
        buckets, skipped = agg.temporal(studies, tf, gran)
        skipped_total += skipped
        per_series.append((label, buckets, len(studies)))

    all_keys = sorted({k for _, b, _ in per_series for k in b})
    keys: list[str] = []
    if all_keys:
        first, last = all_keys[0], all_keys[-1]
        if tf == "start_date":
            # an explicit year filter defines the axis, so leading/trailing empty years show as zeros
            year_key = {"year": "{}", "quarter": "{}-Q1", "month": "{}-01"}[gran]
            end_key = {"year": "{}", "quarter": "{}-Q4", "month": "{}-12"}[gran]
            if plan.filters.start_year_from:
                first = min(first, year_key.format(plan.filters.start_year_from))
            if plan.filters.start_year_to:
                last = max(last, end_key.format(plan.filters.start_year_to))
        keys = ex.bucket_range(first, last, gran)

    rows = []
    projected = 0
    for label, buckets, denom in per_series:
        for k in keys:
            b = buckets.get(k)
            start_iso = _PERIOD_START[gran](k)
            n = b.count if b else 0
            row: dict[str, Any] = {"period": k, "period_start": start_iso, COUNT: n}
            row.update(_period_flags(start_iso, gran, today))
            if row["is_projected"]:
                projected += n
            if plan.metric == "percent":
                row[PERCENT] = pct(n, denom)
            if label is not None:
                row[GROUP] = label
            row.update(cites.for_bucket(b))
            rows.append(row)

    if skipped_total:
        warnings.append(f"{skipped_total} trial(s) had no usable {_TIME_TITLES[tf]} at {gran} precision and are not plotted.")
    if projected:
        warnings.append(
            f"{projected} trial(s) have anticipated (future) {_TIME_TITLES[tf]}s; those periods are flagged is_projected."
        )
    enc = {
        "x": Channel(field="period_start", type="temporal", title=f"{_TIME_TITLES[tf].capitalize()} ({gran})", time_unit=gran, format={"year": "%Y", "quarter": "%Y Q%q", "month": "%b %Y"}[gran]),
        "y": value_channel(plan),
    }
    grouping = None
    if len(series) > 1 or (series and series[0][0] is not None):
        enc["color"] = Channel(
            field=GROUP,
            type="nominal",
            title=dim_title(split_dim) if split_dim else "Comparison group",
            sort=[s[0] for s in series],
        )
        grouping = dim_title(split_dim) if split_dim else "comparison group"
    core = f"Trials per {gran} by {_TIME_TITLES[tf]}"
    if split_dim:
        core += f", by {dim_title(split_dim).lower()}"
    title, sub = title_with(core, plan, datasets)
    return BuildResult(
        Visualization(type="time_series", title=title, subtitle=sub, encoding=enc, data=rows),
        Rendering(
            time_granularity=gran,
            sort="ascending by period",
            units={COUNT: "trials"},
            value_field=PERCENT if plan.metric == "percent" else COUNT,
            grouping=grouping,
            missing_values="periods with no trials are explicit zero rows",
        ),
        f"Each trial is counted once, in the {gran} of its {_TIME_TITLES[tf]}.",
        "The question is about change over time, so trial counts are bucketed by period and drawn as a line.",
        warnings,
    )


def _histogram(plan: QueryPlan, datasets: list[Dataset], cites: CitationFactory) -> BuildResult:
    nf = plan.numeric_field or "enrollment"
    title_txt, unit = ex.NUMERIC_META[nf]
    pts_by_group: dict[str | None, list[agg.NumericPoint]] = {}
    missing = 0
    for d in datasets:
        pts, miss = agg.numeric_values(d.studies, nf)
        pts_by_group[d.label] = pts
        missing += miss
    integer = nf != "duration_months"
    template, per_group = agg.histogram(pts_by_group, integer)
    rows = []
    fmt = (lambda v: f"{int(v):,}") if integer else (lambda v: f"{v:g}")
    for g, bins in per_group.items():
        denom = len(pts_by_group[g])
        for hb in bins:
            if hb.end is None:
                label = f"≥ {fmt(hb.start)}"
            elif integer:
                label = f"{fmt(hb.start)}–{fmt(hb.end - 1)}" if hb.end - hb.start > 1 else fmt(hb.start)
            else:
                label = f"{fmt(hb.start)}–<{fmt(hb.end)}"
            n = hb.bucket.count
            row: dict[str, Any] = {"bin_start": hb.start, "bin_end": hb.end, "bin_label": label, COUNT: n, "is_overflow": hb.end is None}
            if plan.metric == "percent":
                row[PERCENT] = pct(n, denom)
            if g is not None:
                row[GROUP] = g
            row.update(cites.for_bucket(hb.bucket if n else None))
            rows.append(row)
    enc = {
        "x": Channel(field="bin_start", type="quantitative", title=title_txt, unit=unit),
        "x2": Channel(field="bin_end", type="quantitative", title=title_txt, unit=unit),
        "y": value_channel(plan),
        "label": Channel(field="bin_label", type="ordinal", title="Bin", sort="data"),
    }
    if plan.groups:
        enc["color"] = group_channel(plan)
    warnings = [f"{missing} trial(s) lack {title_txt.lower()} and are excluded."] if missing else []
    if template and template[-1].end is None:
        warnings.append(f"Values ≥ {fmt(template[-1].start)} (above the 95th percentile) are folded into one overflow bin (bin_end = null).")
    title, sub = title_with(f"Distribution of {title_txt.lower()}", plan, datasets)
    return BuildResult(
        Visualization(type="histogram", title=title, subtitle=sub, encoding=enc, data=rows),
        Rendering(sort="ascending by bin_start", units={COUNT: "trials", "bin_start": unit, "bin_end": unit}, value_field=COUNT, grouping="comparison group" if plan.groups else None, missing_values="bins are half-open [bin_start, bin_end); bin_end=null marks the overflow bin"),
        "Each trial with a value is counted in exactly one bin.",
        f"{title_txt} is a numeric attribute; a histogram shows its distribution across trials.",
        warnings,
    )


def _axis(nf: str, values: list[Any]) -> Channel:
    title, unit = ex.NUMERIC_META[nf]
    if nf == "start_date":
        return Channel(field=nf, type="temporal", title=title, format="%Y")
    nums = [v for v in values if isinstance(v, (int, float))]
    skewed = bool(nums) and max(nums) > 20 * (sorted(nums)[len(nums) // 2] or 1)
    return Channel(field=nf, type="quantitative", title=title, unit=unit, scale="symlog" if skewed else "linear")


def _scatter(plan: QueryPlan, datasets: list[Dataset], cites: CitationFactory) -> BuildResult:
    xf, yf = plan.x_field or "start_date", plan.y_field or "enrollment"
    rows = []
    dropped = 0
    for d in datasets:
        for s in d.studies:
            hx, hy = ex.numeric(s, xf), ex.numeric(s, yf)
            if hx is None or hy is None:
                dropped += 1
                continue
            nct = ex.nct_id(s)
            row: dict[str, Any] = {"nct_id": nct, "brief_title": ex.brief_title(s), xf: hx[0], yf: hy[0]}
            if d.label is not None:
                row[GROUP] = d.label
            if cites.mode != "off":
                row["citation_count"] = 1
                row["citations"] = [cites.citation(nct, hx[1] + hy[1])]
            rows.append(row)
    enc = {
        "x": _axis(xf, [r[xf] for r in rows]),
        "y": _axis(yf, [r[yf] for r in rows]),
        "tooltip": Channel(field="brief_title", type="nominal", title="Trial"),
        "detail": Channel(field="nct_id", type="nominal", title="NCT ID"),
    }
    if plan.groups:
        enc["color"] = group_channel(plan)
    warnings = [f"{dropped} trial(s) lack {ex.NUMERIC_META[xf][0].lower()} or {ex.NUMERIC_META[yf][0].lower()} and are not plotted."] if dropped else []
    title, sub = title_with(f"{ex.NUMERIC_META[yf][0]} vs {ex.NUMERIC_META[xf][0].lower()}", plan, datasets)
    return BuildResult(
        Visualization(type="scatter_plot", title=title, subtitle=sub, encoding=enc, data=rows),
        Rendering(units={xf: ex.NUMERIC_META[xf][1], yf: ex.NUMERIC_META[yf][1]}, sort="API order", grouping="comparison group" if plan.groups else None),
        "One point per trial.",
        "Relating two numeric trial attributes: a scatter plot with one point per trial.",
        warnings,
    )


_ENTITY_TITLES = {k: v.title for k, v in ex.DIMENSIONS.items()}


def _network(plan: QueryPlan, datasets: list[Dataset], cites: CitationFactory) -> BuildResult:
    src, tgt = plan.network_source or "sponsor", plan.network_target or "drug"
    warnings: list[str] = []
    if plan.groups:
        warnings.append("Comparison groups were merged into a single network.")
    studies, seen = [], set()
    for d in datasets:
        for s in d.studies:
            if ex.nct_id(s) not in seen:
                seen.add(ex.nct_id(s))
                studies.append(s)
    top_n = plan.top_n or 30
    net = agg.network(studies, src, tgt, top_n)
    nodes = []
    degree: dict[str, int] = {}
    for a, b in net.edges:
        degree[a] = degree.get(a, 0) + 1
        degree[b] = degree.get(b, 0) + 1
    for nid, b in sorted(net.nodes.items(), key=lambda kv: (-kv[1].count, kv[1].label)):
        node = {"id": nid, "label": b.label, "entity_type": net.node_type[nid], COUNT: b.count, "degree": degree.get(nid, 0)}
        node.update(cites.for_bucket(b))
        nodes.append(node)
    edges = []
    for (a, b_), bucket in sorted(net.edges.items(), key=lambda kv: (-kv[1].count, kv[0])):
        edge = {"source": a, "target": b_, COUNT: bucket.count}
        edge.update(cites.for_bucket(bucket))
        edges.append(edge)
    co = src == tgt
    if net.min_edge_weight > 1:
        warnings.append(f"Only edges shared by ≥ {net.min_edge_weight} trials are shown (graph legibility).")
    if not edges:
        warnings.append("No co-occurring entities were found among the top nodes.")
    enc = {
        "node_id": Channel(field="id", type="nominal", title="Node id"),
        "node_label": Channel(field="label", type="nominal", title="Entity"),
        "node_size": Channel(field=COUNT, type="quantitative", title="Trials involving entity", unit="trials", scale="sqrt"),
        "node_color": Channel(field="entity_type", type="nominal", title="Entity type", sort=[src] if co else [src, tgt]),
        "edge_source": Channel(field="source", type="nominal", title="Source node id"),
        "edge_target": Channel(field="target", type="nominal", title="Target node id"),
        "edge_weight": Channel(field=COUNT, type="quantitative", title="Trials shared", unit="trials"),
    }
    if co:
        core = f"{_ENTITY_TITLES[src]} co-occurrence network"
        why = f"Which {_ENTITY_TITLES[src].lower()}s appear together in the same trials is a relationship question: nodes are entities, edges link entities that share trials."
        rule = "Node weight = trials listing the entity; edge weight = trials listing both entities."
    else:
        core = f"{_ENTITY_TITLES[src]} ↔ {_ENTITY_TITLES[tgt].lower()} network"
        why = f"Relationships between {_ENTITY_TITLES[src].lower()}s and {_ENTITY_TITLES[tgt].lower()}s: a bipartite network linking each pair that shares trials."
        rule = f"Node weight = trials involving the entity; edge weight = trials involving both the {src} and the {tgt}."
    if src == "drug" or tgt == "drug":
        rule += " Drug names are normalised (dose/route/parentheticals removed, combinations split); placebo/standard-of-care arms are excluded."
    title, sub = title_with(core, plan, datasets)
    return BuildResult(
        Visualization(type="network_graph", title=title, subtitle=sub, encoding=enc, data=NetworkData(nodes=nodes, edges=edges)),
        Rendering(units={COUNT: "trials"}, value_field=COUNT, categories_total=net.total_nodes, categories_shown=len(nodes), sort="nodes and edges descending by trial_count"),
        rule,
        why,
        warnings,
    )


def _trial_list(plan: QueryPlan, datasets: list[Dataset], cites: CitationFactory) -> BuildResult:
    nf = plan.numeric_field or "enrollment"
    rows_src = []
    for d in datasets:
        for s in d.studies:
            hit = ex.numeric(s, nf)
            if hit is not None:
                rows_src.append((hit[0], s, d.label, hit[1]))
    rows_src.sort(key=lambda r: (r[0], ex.nct_id(r[1])), reverse=True)
    top_n = plan.top_n or 25
    rows = []
    for rank, (value, s, label, evs) in enumerate(rows_src[:top_n], start=1):
        nct = ex.nct_id(s)
        phase = ex.DIMENSIONS["phase"].extract(s)[0]
        status = ex.DIMENSIONS["overall_status"].extract(s)[0]
        sponsor = ex.DIMENSIONS["sponsor"].extract(s)[0]
        row: dict[str, Any] = {
            "rank": rank,
            "nct_id": nct,
            "brief_title": ex.brief_title(s),
            "phase": phase.label,
            "overall_status": status.label,
            "lead_sponsor": sponsor.label,
            nf: value,
        }
        if label is not None:
            row[GROUP] = label
        if cites.mode != "off":
            row["citation_count"] = 1
            row["citations"] = [cites.citation(nct, evs + phase.evidence + status.evidence + sponsor.evidence)]
        rows.append(row)
    title_txt, unit = ex.NUMERIC_META[nf]
    enc = {
        "rank": Channel(field="rank", type="ordinal", title="#"),
        "nct_id": Channel(field="nct_id", type="nominal", title="NCT ID"),
        "brief_title": Channel(field="brief_title", type="nominal", title="Title"),
        "phase": Channel(field="phase", type="ordinal", title="Phase"),
        "overall_status": Channel(field="overall_status", type="nominal", title="Status"),
        "lead_sponsor": Channel(field="lead_sponsor", type="nominal", title="Lead sponsor"),
        nf: Channel(field=nf, type="temporal" if nf == "start_date" else "quantitative", title=title_txt, unit=unit, sort="descending"),
    }
    if plan.groups:
        enc[GROUP] = group_channel(plan)
    title, sub = title_with(f"Top {len(rows)} trials by {title_txt.lower()}", plan, datasets)
    return BuildResult(
        Visualization(type="table", title=title, subtitle=sub, encoding=enc, data=rows),
        Rendering(sort=f"descending by {nf}", units={nf: unit}, value_field=nf),
        "One row per trial.",
        "The question asks for specific trials rather than an aggregate, so a ranked table is the clearest answer.",
    )
