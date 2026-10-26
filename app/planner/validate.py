"""Plan validation, normalisation and request-field overrides.

Three layers, applied in order by :class:`app.planner.Planner`:

1. :func:`check_plan` - hard constraints. Violations raise
   :class:`PlanValidationError`, whose messages are fed back to the LLM for one
   repair attempt.
2. :func:`normalize_plan` - soft fixes with a warning (fill defaults, convert
   equivalent analyses, clamp ranges, clear irrelevant fields).
3. :func:`apply_request_overrides` - structured request fields win over
   anything inferred from text.
"""

from __future__ import annotations

from ..schemas.plan import Filters, Group, QueryPlan
from ..schemas.request import QueryRequest

DEFAULT_TOP_N = {
    "drug": 20, "intervention": 20, "condition": 20, "sponsor": 15, "collaborator": 15,
    "country": 20, "facility": 20, "investigator": 20,
}

COMPATIBLE_VIZ = {
    "total": {"kpi", "bar_chart"},
    "distribution": {"bar_chart", "grouped_bar_chart", "stacked_bar_chart", "choropleth_map"},
    "cross_tab": {"stacked_bar_chart", "heatmap"},
    "time_trend": {"time_series"},
    "histogram": {"histogram"},
    "scatter": {"scatter_plot"},
    "network": {"network_graph"},
    "trial_list": {"table"},
}
VIZ_PREFERENCE = {
    "heatmap": "heatmap",
    "choropleth_map": "map",
    "stacked_bar_chart": "stacked",
    "grouped_bar_chart": "grouped",
    "time_series": "line",
}
COMPARE_FIELD_TO_FILTER = {
    "drug": "intervention", "condition": "condition", "sponsor": "sponsor", "country": "country", "term": "term",
}


class PlanValidationError(ValueError):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def check_plan(plan: QueryPlan) -> None:
    errors: list[str] = []
    a = plan.analysis
    if a == "distribution" and not plan.dimension:
        errors.append("analysis 'distribution' requires 'dimension'")
    if a == "cross_tab":
        if not plan.dimension or not plan.series_dimension:
            errors.append("analysis 'cross_tab' requires both 'dimension' and 'series_dimension'")
        elif plan.dimension == plan.series_dimension:
            errors.append("'dimension' and 'series_dimension' must differ")
    if a == "histogram":
        if not plan.numeric_field:
            errors.append("analysis 'histogram' requires 'numeric_field'")
        elif plan.numeric_field == "start_date":
            errors.append("'start_date' is temporal; use analysis 'time_trend' for counts over time")
    if a == "scatter":
        if not plan.x_field or not plan.y_field:
            errors.append("analysis 'scatter' requires 'x_field' and 'y_field'")
        elif plan.x_field == plan.y_field:
            errors.append("'x_field' and 'y_field' must differ")
    if a == "network" and (not plan.network_source or not plan.network_target):
        errors.append("analysis 'network' requires 'network_source' and 'network_target'")
    if len(plan.groups) == 1:
        errors.append("'groups' must be empty or contain 2-6 groups")
    if len(plan.groups) > 6:
        errors.append("at most 6 comparison groups are supported")
    labels = [g.label.strip().lower() for g in plan.groups]
    if len(set(labels)) != len(labels):
        errors.append("group labels must be unique")
    for g in plan.groups:
        if g.filters.is_empty():
            errors.append(f"group '{g.label}' has no filters")
    for f in [plan.filters] + [g.filters for g in plan.groups]:
        if f.start_year_from and f.start_year_to and f.start_year_from > f.start_year_to:
            errors.append("start_year_from must be <= start_year_to")
        for y in (f.start_year_from, f.start_year_to):
            if y is not None and not 1900 <= y <= 2100:
                errors.append(f"year {y} out of range 1900-2100")
    if errors:
        raise PlanValidationError(errors)


def normalize_plan(plan: QueryPlan) -> tuple[QueryPlan, list[str]]:
    p = plan.model_copy(deep=True)
    notes: list[str] = []

    if p.analysis == "distribution" and p.dimension in ("start_year", "completion_year"):
        p.time_field = "start_date" if p.dimension == "start_year" else "completion_date"
        p.analysis, p.dimension = "time_trend", None
        notes.append("Counting trials per year is treated as a time trend.")
    if p.analysis == "cross_tab" and p.groups:
        p.analysis, p.series_dimension = "distribution", None
        notes.append("Comparison groups already form the series, so the second dimension was dropped.")
    if p.analysis == "time_trend":
        p.time_field = p.time_field or "start_date"
        p.time_granularity = p.time_granularity or "year"
        if p.series_dimension in ("start_year", "completion_year"):
            p.series_dimension = None
    if p.analysis == "trial_list":
        p.numeric_field = p.numeric_field or "enrollment"
    if p.chart_preference == "map" and not (p.analysis == "distribution" and p.dimension == "country" and not p.groups):
        p.chart_preference = "auto"
        notes.append("A map is only available for a single (non-compared) distribution by country.")
    if p.chart_preference == "heatmap" and p.analysis != "cross_tab":
        p.chart_preference = "auto"
        notes.append("A heatmap needs two categorical dimensions (cross_tab).")

    # clear fields that the chosen analysis does not use (keeps the echoed plan honest)
    uses = {
        "dimension": {"distribution", "cross_tab"},
        "series_dimension": {"cross_tab", "time_trend"},
        "time_field": {"time_trend"},
        "time_granularity": {"time_trend"},
        "numeric_field": {"histogram", "trial_list"},
        "x_field": {"scatter"},
        "y_field": {"scatter"},
        "network_source": {"network"},
        "network_target": {"network"},
    }
    for fname, analyses in uses.items():
        if p.analysis not in analyses:
            setattr(p, fname, None)

    if p.top_n is not None and not 1 <= p.top_n <= 100:
        p.top_n = max(1, min(100, p.top_n))
        notes.append("top_n clamped to 1-100.")
    if p.top_n is None:
        if p.analysis == "network":
            p.top_n = 30
        elif p.analysis == "trial_list":
            p.top_n = 25
        elif p.analysis in ("distribution", "cross_tab") and p.dimension in DEFAULT_TOP_N:
            p.top_n = DEFAULT_TOP_N[p.dimension]
    return p, notes


def apply_request_overrides(plan: QueryPlan, req: QueryRequest) -> tuple[QueryPlan, list[str]]:
    p = plan.model_copy(deep=True)
    notes: list[str] = []
    f = p.filters.model_dump()
    explicit = {
        "intervention": req.drug_name,
        "condition": req.condition,
        "sponsor": req.sponsor,
        "country": req.country,
        "term": req.keyword,
        "phases": req.trial_phase,
        "statuses": req.overall_status,
        "study_type": req.study_type,
        "intervention_types": req.intervention_type,
        "sponsor_classes": req.sponsor_class,
        "start_year_from": req.start_year,
        "start_year_to": req.end_year,
    }
    for key, value in explicit.items():
        if value not in (None, []):
            if f.get(key) not in (None, [], value):
                notes.append(f"Request field overrides inferred {key}={f[key]!r} with {value!r}.")
            f[key] = value
    p.filters = Filters.model_validate(f)

    if req.compare:
        fkey = COMPARE_FIELD_TO_FILTER[req.compare.field]
        p.groups = [Group(label=v, filters=Filters(**{fkey: v})) for v in req.compare.values]
        if getattr(p.filters, fkey):
            notes.append(f"Base filter {fkey} removed because it is the comparison field.")
            setattr(p.filters, fkey, None)
        if p.analysis == "cross_tab":
            p.analysis, p.series_dimension = "distribution", None
        if p.analysis == "total":
            notes.append("Comparison requested: showing one count per group.")
    elif p.groups:
        # a group must not be contradicted by a base text filter on the same field
        for g in p.groups:
            for key in g.filters.text_items():
                if getattr(p.filters, key) and key != "term":
                    setattr(p.filters, key, None)

    if req.top_n:
        p.top_n = req.top_n
    if req.visualization_type:
        wanted = req.visualization_type
        if wanted in COMPATIBLE_VIZ[p.analysis]:
            p.chart_preference = VIZ_PREFERENCE.get(wanted, p.chart_preference)
            if wanted == "bar_chart" and p.analysis == "distribution" and p.groups:
                notes.append("bar_chart with comparison groups is rendered as grouped_bar_chart.")
        else:
            notes.append(
                f"Requested visualization_type '{wanted}' cannot answer a '{p.analysis}' question; "
                f"using {sorted(COMPATIBLE_VIZ[p.analysis])[0]} instead."
            )
    return p, notes
