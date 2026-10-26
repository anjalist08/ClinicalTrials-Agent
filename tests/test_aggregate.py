from fixtures.factory import corpus, study

from app.analytics import aggregate as agg
from app.analytics.citations import CitationFactory
from app.ctgov import extract as ex


def test_categorical_counts_each_trial_once_per_bucket():
    buckets = agg.categorical(corpus(), "country")
    us = buckets["united states"]
    # NCT90000001 has two US sites but is counted once
    assert us.count == 3
    assert us.nct_ids == ["NCT90000001", "NCT90000004", "NCT90000007"]


def test_phase_distribution_totals_match_corpus():
    buckets = agg.categorical(corpus(), "phase")
    assert sum(b.count for b in buckets.values()) == len(corpus())  # phase is single-valued per trial
    assert buckets["phase 3"].count == 2


def test_ordinal_dimension_order():
    buckets = agg.categorical(corpus(), "phase")
    labels = [buckets[k].label for k in agg.order_keys(buckets, "phase", None)]
    assert labels == ["Phase 1/2", "Phase 2", "Phase 2/3", "Phase 3", "Not Applicable", "Not specified"]


def test_nominal_order_by_count_then_label_and_top_n():
    buckets = agg.categorical(corpus(), "sponsor")
    keys = agg.order_keys(buckets, "sponsor", 2)
    # three sponsors tie at 2 trials each -> alphabetical tie-break keeps output deterministic
    assert [buckets[k].label for k in keys] == ["Bristol-Myers Squibb", "Eisai Inc."]


def test_label_is_most_common_surface_form():
    studies = [
        study("NCT91", "a", interventions=[("DRUG", "pembrolizumab")]),
        study("NCT92", "b", interventions=[("DRUG", "Pembrolizumab")]),
        study("NCT93", "c", interventions=[("DRUG", "Pembrolizumab 10 mg/kg")]),
    ]
    b = agg.categorical(studies, "drug")["pembrolizumab"]
    assert b.count == 3 and b.label == "Pembrolizumab"


def test_temporal_skips_undated():
    buckets, skipped = agg.temporal(corpus(), "start_date", "year")
    assert skipped == 1
    assert {k: b.count for k, b in buckets.items()} == {"2015": 1, "2016": 2, "2018": 2, "2019": 1, "2020": 1}


def test_histogram_bins_cover_all_values_once():
    pts, missing = agg.numeric_values(corpus(), "enrollment")
    assert missing == 1
    template, per_group = agg.histogram({None: pts}, integer=True)
    bins = per_group[None]
    assert sum(b.bucket.count for b in bins) == len(pts)
    for b in bins:
        for nct in b.bucket.nct_ids:
            v = next(p.value for p in pts if p.nct_id == nct)
            assert v >= b.start and (b.end is None or v < b.end)


def test_histogram_overflow_bin_for_long_tail():
    studies = [study(f"NCT9{i:04d}", "x", enrollment=(10 + i, "ACTUAL")) for i in range(60)]
    studies.append(study("NCT99999", "huge", enrollment=(100000, "ACTUAL")))
    pts, _ = agg.numeric_values(studies, "enrollment")
    _, per_group = agg.histogram({None: pts}, integer=True)
    bins = per_group[None]
    assert bins[-1].end is None and bins[-1].bucket.nct_ids == ["NCT99999"]
    assert len(bins) <= 22


def test_shared_bins_across_groups():
    a = [study("NCT1", "a", enrollment=(10, "ACTUAL"))]
    b = [study("NCT2", "b", enrollment=(500, "ACTUAL"))]
    pa, _ = agg.numeric_values(a, "enrollment")
    pb, _ = agg.numeric_values(b, "enrollment")
    _, per = agg.histogram({"A": pa, "B": pb}, integer=True)
    assert [x.start for x in per["A"]] == [x.start for x in per["B"]]


def test_cooccurrence_network_edges_and_weights():
    net = agg.network(corpus(), "drug", "drug", top_n=10)
    pairs = {tuple(sorted((a.split(":")[1], b.split(":")[1]))): e.count for (a, b), e in net.edges.items()}
    assert pairs[("lenvatinib", "pembrolizumab")] == 1
    assert pairs[("ipilimumab", "nivolumab")] == 1
    assert pairs[("docetaxel", "nivolumab")] == 1
    # an edge cites the evidence for both endpoints
    edge = next(e for (a, b), e in net.edges.items() if "ipilimumab" in a + b)
    assert {ev.excerpt for ev in edge.evidence["NCT90000004"]} == {"Nivolumab", "Ipilimumab"}


def test_bipartite_network_uses_typed_node_ids():
    net = agg.network(corpus(), "sponsor", "drug", top_n=20)
    assert all(nid.split(":")[0] in ("sponsor", "drug") for nid in net.nodes)
    assert all(a.startswith("sponsor:") and b.startswith("drug:") for a, b in net.edges)


def test_network_threshold_limits_edges():
    studies = [
        study(f"NCT8{i:04d}", "x", interventions=[("DRUG", f"Drug{j}") for j in range(i % 7, i % 7 + 6)])
        for i in range(40)
    ]
    net = agg.network(studies, "drug", "drug", top_n=12, max_edges=10)
    assert len(net.edges) <= 10 and net.min_edge_weight > 1


def test_citation_factory_modes():
    studies = corpus()
    bucket = agg.categorical(studies, "country")["united states"]
    by_id = {ex.nct_id(s): s for s in studies}
    summary = CitationFactory(by_id, {"intervention": "pembrolizumab"}, "summary", 2).for_bucket(bucket)
    assert summary["citation_count"] == 3 and len(summary["citations"]) == 2
    c = summary["citations"][0]
    assert c["url"] == "https://clinicaltrials.gov/study/NCT90000001"
    assert c["match_evidence"][0]["excerpt"] == "Pembrolizumab 200 mg IV"
    full = CitationFactory(by_id, {}, "full", 2).for_bucket(bucket)
    assert len(full["citations"]) == 3
    off = CitationFactory(by_id, {}, "off", 2).for_bucket(bucket)
    assert off == {"citation_count": 3}
