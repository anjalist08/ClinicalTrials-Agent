import pytest

from app.planner.grounding import ground_plan, is_grounded
from app.planner.validate import PlanValidationError, apply_request_overrides, check_plan, normalize_plan
from app.schemas.plan import Filters, Group, QueryPlan
from app.schemas.request import QueryRequest


def P(**kw):
    return QueryPlan(interpretation="x", **kw)


# ---------------------------------------------------------------- hard validation
@pytest.mark.parametrize(
    "plan,msg",
    [
        (P(analysis="distribution"), "requires 'dimension'"),
        (P(analysis="cross_tab", dimension="phase", series_dimension="phase"), "must differ"),
        (P(analysis="histogram", numeric_field="start_date"), "time_trend"),
        (P(analysis="scatter", x_field="enrollment"), "x_field"),
        (P(analysis="network", network_source="drug"), "network_target"),
        (P(analysis="distribution", dimension="phase", groups=[Group(label="a", filters=Filters(term="a"))]), "2-6"),
        (P(analysis="distribution", dimension="phase", groups=[Group(label="a", filters=Filters(term="a")), Group(label="A", filters=Filters(term="b"))]), "unique"),
        (P(analysis="distribution", dimension="phase", groups=[Group(label="a", filters=Filters()), Group(label="b", filters=Filters(term="b"))]), "no filters"),
        (P(analysis="total", filters=Filters(start_year_from=2020, start_year_to=2010)), "<="),
    ],
)
def test_check_plan_rejects(plan, msg):
    with pytest.raises(PlanValidationError) as e:
        check_plan(plan)
    assert msg in str(e.value)


# ---------------------------------------------------------------- normalisation
def test_normalize_converts_year_distribution_to_time_trend():
    p, notes = normalize_plan(P(analysis="distribution", dimension="start_year"))
    assert p.analysis == "time_trend" and p.time_field == "start_date" and p.time_granularity == "year" and notes


def test_normalize_clears_unused_fields_and_defaults_top_n():
    p, _ = normalize_plan(P(analysis="distribution", dimension="sponsor", x_field="enrollment", network_source="drug"))
    assert p.x_field is None and p.network_source is None and p.top_n == 15


def test_normalize_drops_map_for_non_country():
    p, notes = normalize_plan(P(analysis="distribution", dimension="phase", chart_preference="map"))
    assert p.chart_preference == "auto" and notes


# ---------------------------------------------------------------- grounding
def test_is_grounded_exact_token_and_fuzzy():
    assert is_grounded("pembrolizumab", ["trials for Pembrolizumab?"])
    assert is_grounded("breast cancer", ["How are cancer trials of the breast doing"])  # token-wise
    assert is_grounded("pembrolizumab", ["trials for pembrolizumb"])  # typo
    assert not is_grounded("nivolumab", ["trials for pembrolizumab"])
    assert not is_grounded("Keytruda", ["trials for pembrolizumab"])


def test_boolean_terms_grounded_per_operand():
    q = ["How has pembrolizumab use in lung cancer trials changed per year?"]
    assert is_grounded("pembrolizumab AND (lung cancer)", q)
    assert not is_grounded("pembrolizumab AND (breast cancer)", q)


def test_rules_plan_with_two_terms_survives_grounding():
    from app.planner.rules import plan_with_rules

    req = QueryRequest(query="How has pembrolizumab use in lung cancer trials changed per year?")
    plan, warnings = ground_plan(plan_with_rules(req.query, 2026), req)
    assert plan.filters.term == "pembrolizumab AND (lung cancer)" and not warnings


def test_grounding_drops_hallucinated_filters():
    req = QueryRequest(query="How are pembrolizumab trials distributed across sponsors?")
    hallucinated = P(
        analysis="distribution",
        dimension="sponsor",
        filters=Filters(intervention="Keytruda", condition="melanoma", statuses=["RECRUITING"], start_year_from=2015, phases=["PHASE3"]),
    )
    plan, warnings = ground_plan(hallucinated, req)
    assert plan.filters.intervention is None and plan.filters.condition is None
    assert plan.filters.statuses == [] and plan.filters.start_year_from is None and plan.filters.phases == []
    assert len(warnings) == 5


def test_grounding_keeps_supported_filters_and_structured_fields():
    req = QueryRequest(query="Recruiting phase 3 trials since 2018 in the US for this drug", drug_name="Keytruda")
    plan = P(
        analysis="total",
        filters=Filters(intervention="Keytruda", statuses=["RECRUITING"], phases=["PHASE3"], start_year_from=2018, country="United States"),
    )
    grounded, warnings = ground_plan(plan, req)
    assert grounded.filters == plan.filters and warnings == []


def test_grounding_removes_comparison_with_ungrounded_side():
    req = QueryRequest(query="Compare phases for nivolumab vs pembrolizumab")
    plan = P(
        analysis="distribution",
        dimension="phase",
        groups=[Group(label="nivolumab", filters=Filters(intervention="nivolumab")),
                Group(label="atezolizumab", filters=Filters(intervention="atezolizumab"))],
    )
    grounded, warnings = ground_plan(plan, req)
    assert grounded.groups == [] and any("Comparison removed" in w for w in warnings)


# ---------------------------------------------------------------- overrides
def test_request_fields_override_plan():
    req = QueryRequest(query="trials by phase", drug_name="Pembrolizumab", trial_phase=["3"], start_year=2015, top_n=5)
    p, notes = apply_request_overrides(P(analysis="distribution", dimension="phase", filters=Filters(intervention="other")), req)
    assert p.filters.intervention == "Pembrolizumab" and p.filters.phases == ["PHASE3"]
    assert p.filters.start_year_from == 2015 and p.top_n == 5 and notes


def test_compare_field_builds_groups_and_clears_base_filter():
    req = QueryRequest(query="phases", compare={"field": "drug", "values": ["nivolumab", "pembrolizumab"]})
    p, _ = apply_request_overrides(P(analysis="distribution", dimension="phase", filters=Filters(intervention="nivolumab")), req)
    assert [g.filters.intervention for g in p.groups] == ["nivolumab", "pembrolizumab"]
    assert p.filters.intervention is None


def test_incompatible_visualization_type_is_reported_not_forced():
    req = QueryRequest(query="trials per year", visualization_type="network_graph")
    p, notes = apply_request_overrides(P(analysis="time_trend"), req)
    assert p.analysis == "time_trend" and any("cannot answer" in n for n in notes)


def test_compatible_visualization_type_sets_preference():
    req = QueryRequest(query="phase by status", visualization_type="heatmap")
    p, _ = apply_request_overrides(P(analysis="cross_tab", dimension="phase", series_dimension="overall_status"), req)
    assert p.chart_preference == "heatmap"


def test_combination_studies_do_not_become_filters():
    # seen with gpt-4.1-mini on live runs: "combination studies" -> intervention="combination" + COMBINATION_PRODUCT
    req = QueryRequest(query="Which drugs frequently co-occur in combination studies for pancreatic cancer?")
    plan = P(
        analysis="network", network_source="drug", network_target="drug",
        filters=Filters(condition="pancreatic cancer", intervention="combination", intervention_types=["COMBINATION_PRODUCT"]),
    )
    grounded, warnings = ground_plan(plan, req)
    assert grounded.filters.condition == "pancreatic cancer"
    assert grounded.filters.intervention is None and grounded.filters.intervention_types == []
    assert len(warnings) == 2


def test_intervention_types_need_their_own_cue():
    req = QueryRequest(query="Which vaccine and device trials exist for flu?")
    plan = P(analysis="total", filters=Filters(intervention_types=["BIOLOGICAL", "DEVICE", "DRUG"]))
    grounded, _ = ground_plan(plan, req)
    assert grounded.filters.intervention_types == ["BIOLOGICAL", "DEVICE"]


def test_enum_values_from_structured_fields_are_not_reported_as_dropped():
    # seen live: the LLM copied trial_phase from the structured fields into its plan
    req = QueryRequest(query="Show a network of sponsors and drugs for glioblastoma trials", trial_phase=["2", "3"])
    plan = P(analysis="network", network_source="sponsor", network_target="drug",
             filters=Filters(condition="glioblastoma", phases=["PHASE2", "PHASE3"]))
    grounded, warnings = ground_plan(plan, req)
    assert grounded.filters.phases == ["PHASE2", "PHASE3"] and warnings == []
