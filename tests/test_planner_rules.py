"""The keyword planner must cover every question class in the assignment appendix."""

import pytest

from app.planner.rules import plan_with_rules


def plan(q):
    return plan_with_rules(q, current_year=2026)


@pytest.mark.parametrize(
    "query,analysis,extra",
    [
        # time trends
        ("How has the number of trials for pembrolizumab changed per year since 2015?", "time_trend",
         {"filters.term": "pembrolizumab", "filters.start_year_from": 2015, "time_granularity": "year"}),
        ("How many trials started each year for Alzheimer's disease?", "time_trend",
         {"filters.term": "Alzheimer's disease"}),
        ("Monthly trend of COVID-19 vaccine trials", "time_trend", {"time_granularity": "month"}),
        # distributions
        ("How are breast cancer trials distributed across phases?", "distribution",
         {"dimension": "phase", "filters.term": "breast cancer", "filters.phases": []}),
        ("What are the most common intervention types for melanoma trials?", "distribution",
         {"dimension": "intervention_type"}),
        # comparisons
        ("Compare phases for trials involving nivolumab vs pembrolizumab.", "distribution",
         {"dimension": "phase", "groups": ["nivolumab", "pembrolizumab"]}),
        ("Compare sponsor categories across two conditions.", "cross_tab",
         {"dimension": "sponsor_class", "series_dimension": "condition"}),
        # geography
        ("Which countries have the most recruiting trials for multiple myeloma?", "distribution",
         {"dimension": "country", "filters.statuses": ["RECRUITING"], "filters.term": "multiple myeloma"}),
        ("Show a world map of hepatitis C trials", "distribution", {"dimension": "country", "chart_preference": "map"}),
        # networks
        ("Show a network of sponsors ↔ drugs for glioblastoma trials.", "network",
         {"network_source": "sponsor", "network_target": "drug", "filters.term": "glioblastoma"}),
        ("Which drugs frequently co-occur in combination studies?", "network",
         {"network_source": "drug", "network_target": "drug"}),
        # others
        ("What is the distribution of enrollment sizes for phase 3 diabetes trials?", "histogram",
         {"numeric_field": "enrollment", "filters.phases": ["PHASE3"]}),
        ("Scatter of enrollment vs duration for obesity trials", "scatter",
         {"x_field": "duration_months", "y_field": "enrollment"}),
        ("Show the largest completed trials for COVID-19 vaccines", "trial_list",
         {"numeric_field": "enrollment", "filters.statuses": ["COMPLETED"]}),
        ("How many recruiting trials are there for ALS in Germany?", "total",
         {"filters.country": "Germany", "filters.term": "ALS"}),
        ("Breakdown of lung cancer trials by phase and status", "cross_tab",
         {"dimension": "phase", "series_dimension": "overall_status"}),
    ],
)
def test_question_classes(query, analysis, extra):
    p = plan(query)
    assert p.analysis == analysis
    for key, expected in extra.items():
        if key == "groups":
            assert [g.label for g in p.groups] == expected
            continue
        obj = p
        for part in key.split("."):
            obj = getattr(obj, part)
        assert obj == expected, f"{key}: {obj!r} != {expected!r}"


def test_years_relative_and_ranges():
    assert plan("trials for asthma in the last 5 years per year").filters.start_year_from == 2021
    p = plan("trials started between 2010 and 2015 for gout by phase")
    assert (p.filters.start_year_from, p.filters.start_year_to) == (2010, 2015)
    assert plan("hiv trials before 2000 by phase").filters.start_year_to == 1999


def test_phase_filter_vs_dimension():
    assert plan("Phase 2 and 3 trials for psoriasis by sponsor").filters.phases == ["PHASE2", "PHASE3"]
    assert plan("psoriasis trials by phase").filters.phases == []


def test_percent_metric_and_country_group():
    p = plan("Compare the share of industry-sponsored trials between asthma and COPD")
    assert p.metric == "percent" and p.dimension == "sponsor_class"
    assert [g.label for g in p.groups] == ["asthma", "COPD"]
    p = plan("Compare phases of diabetes trials in France vs Germany")
    assert [g.filters.country for g in p.groups] == ["France", "Germany"]


def test_no_spurious_country_from_condition_words():
    p = plan("How are lung cancer trials distributed by phase?")
    assert p.filters.country is None and p.filters.term == "lung cancer"
