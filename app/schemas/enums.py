"""Controlled vocabularies shared by the request, plan and response schemas.

ClinicalTrials.gov enum values are copied from the API v2 data structure so
that values can be passed to the API (and compared with API responses)
without translation. Human-readable labels live next to them so the
rendering layer never has to guess how to display a raw enum.
"""

from __future__ import annotations

from typing import Literal

# --------------------------------------------------------------------------
# ClinicalTrials.gov enums (API v2 spelling)
# --------------------------------------------------------------------------

Phase = Literal["EARLY_PHASE1", "PHASE1", "PHASE2", "PHASE3", "PHASE4", "NA"]

PHASE_LABELS: dict[str, str] = {
    "EARLY_PHASE1": "Early Phase 1",
    "PHASE1": "Phase 1",
    "PHASE2": "Phase 2",
    "PHASE3": "Phase 3",
    "PHASE4": "Phase 4",
    "NA": "Not Applicable",
}

OverallStatus = Literal[
    "ACTIVE_NOT_RECRUITING",
    "COMPLETED",
    "ENROLLING_BY_INVITATION",
    "NOT_YET_RECRUITING",
    "RECRUITING",
    "SUSPENDED",
    "TERMINATED",
    "WITHDRAWN",
    "AVAILABLE",
    "NO_LONGER_AVAILABLE",
    "TEMPORARILY_NOT_AVAILABLE",
    "APPROVED_FOR_MARKETING",
    "WITHHELD",
    "UNKNOWN",
]

StudyType = Literal["INTERVENTIONAL", "OBSERVATIONAL", "EXPANDED_ACCESS"]

InterventionType = Literal[
    "BEHAVIORAL",
    "BIOLOGICAL",
    "COMBINATION_PRODUCT",
    "DEVICE",
    "DIAGNOSTIC_TEST",
    "DIETARY_SUPPLEMENT",
    "DRUG",
    "GENETIC",
    "PROCEDURE",
    "RADIATION",
    "OTHER",
]

SponsorClass = Literal[
    "NIH", "FED", "OTHER_GOV", "INDIV", "INDUSTRY", "NETWORK", "AMBIG", "OTHER", "UNKNOWN"
]

SPONSOR_CLASS_LABELS: dict[str, str] = {
    "NIH": "NIH",
    "FED": "Other U.S. federal agency",
    "OTHER_GOV": "Other government",
    "INDIV": "Individual",
    "INDUSTRY": "Industry",
    "NETWORK": "Network",
    "AMBIG": "Ambiguous",
    "OTHER": "Other (academic, hospital, non-profit)",
    "UNKNOWN": "Unknown",
}


def enum_label(value: str) -> str:
    """Generic fallback label for an API enum: ``NOT_YET_RECRUITING`` -> ``Not Yet Recruiting``."""
    return value.replace("_", " ").title()


# --------------------------------------------------------------------------
# Planner vocabulary (our own DSL)
# --------------------------------------------------------------------------

Analysis = Literal[
    "total",  # one number (KPI card)
    "distribution",  # count trials per category
    "cross_tab",  # count trials per (category x category)
    "time_trend",  # count trials per time bucket
    "histogram",  # distribution of a numeric trial attribute
    "scatter",  # one point per trial, two numeric attributes
    "network",  # co-occurrence graph between entities
    "trial_list",  # ranked table of individual trials
]

Dimension = Literal[
    "phase",
    "overall_status",
    "study_type",
    "intervention_type",
    "drug",
    "intervention",
    "condition",
    "sponsor",
    "sponsor_class",
    "collaborator",
    "country",
    "facility",
    "investigator",
    "allocation",
    "intervention_model",
    "primary_purpose",
    "masking",
    "sex",
    "age_group",
    "has_results",
    "start_year",
    "completion_year",
]

NetworkEntity = Literal[
    "drug",
    "intervention",
    "intervention_type",
    "condition",
    "sponsor",
    "collaborator",
    "sponsor_class",
    "country",
    "facility",
    "investigator",
    "phase",
]

NumericField = Literal[
    "enrollment",
    "duration_months",
    "num_sites",
    "num_countries",
    "num_interventions",
    "start_date",  # temporal; allowed on scatter axes / trial_list sorting only
]

TimeField = Literal["start_date", "primary_completion_date", "completion_date", "first_posted_date"]

TimeGranularity = Literal["year", "quarter", "month"]

Metric = Literal["count", "percent"]

ChartPreference = Literal["auto", "horizontal_bar", "stacked", "grouped", "heatmap", "map", "line", "area"]

VizType = Literal[
    "kpi",
    "bar_chart",
    "grouped_bar_chart",
    "stacked_bar_chart",
    "heatmap",
    "time_series",
    "histogram",
    "scatter_plot",
    "network_graph",
    "choropleth_map",
    "table",
]

CitationMode = Literal["off", "summary", "full"]

CompareField = Literal["drug", "condition", "sponsor", "country", "term"]
