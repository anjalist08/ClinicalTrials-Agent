"""The query plan: the *only* thing the language model is allowed to produce.

The plan is a small, typed DSL. The model chooses an analysis, dimensions and
filters from closed vocabularies; it never produces numbers, trial IDs or chart
data. Everything downstream (fetching, counting, citing) is deterministic code
operating on ClinicalTrials.gov records, which is what keeps the numbers in the
visualization free of hallucination.

This model is also used as the OpenAI Structured Outputs schema, so it avoids
JSON-schema keywords strict mode does not accept (min/max, patterns). Range and
cross-field checks live in :mod:`app.planner.validate` instead.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from .enums import (
    Analysis,
    ChartPreference,
    Dimension,
    InterventionType,
    Metric,
    NetworkEntity,
    NumericField,
    OverallStatus,
    Phase,
    SponsorClass,
    StudyType,
    TimeField,
    TimeGranularity,
)


class Filters(BaseModel):
    """Constraints on which trials are retrieved. ``None`` / empty means "no constraint"."""

    model_config = ConfigDict(extra="forbid")

    condition: str | None = Field(default=None, description="Condition/disease text, copied verbatim from the question.")
    intervention: str | None = Field(default=None, description="Drug/intervention text, copied verbatim.")
    sponsor: str | None = Field(default=None, description="Sponsor/organization text, copied verbatim.")
    term: str | None = Field(default=None, description="Other free-text keyword, copied verbatim (searched everywhere).")
    country: str | None = Field(default=None, description="Country where trial sites are located.")
    phases: list[Phase] = Field(default_factory=list)
    statuses: list[OverallStatus] = Field(default_factory=list)
    study_type: StudyType | None = None
    intervention_types: list[InterventionType] = Field(default_factory=list)
    sponsor_classes: list[SponsorClass] = Field(default_factory=list)
    start_year_from: int | None = Field(default=None, description="Earliest study start year (inclusive).")
    start_year_to: int | None = Field(default=None, description="Latest study start year (inclusive).")

    def text_items(self) -> dict[str, str]:
        """Free-text filter values (the ones that must be grounded in the user's input)."""
        return {
            k: v
            for k, v in {
                "condition": self.condition,
                "intervention": self.intervention,
                "sponsor": self.sponsor,
                "term": self.term,
                "country": self.country,
            }.items()
            if v
        }

    def is_empty(self) -> bool:
        return not any(self.model_dump(exclude_defaults=True).values())


class Group(BaseModel):
    """One side of a comparison. Its filters are layered on top of the base filters."""

    model_config = ConfigDict(extra="forbid")

    label: str = Field(description="Legend label for this group, e.g. the drug name.")
    filters: Filters


class QueryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    interpretation: str = Field(description="One sentence restating what will be visualized.")
    analysis: Analysis
    dimension: Dimension | None = Field(
        default=None, description="Category axis for distribution / cross_tab."
    )
    series_dimension: Dimension | None = Field(
        default=None, description="Second category (cross_tab colour/rows, or optional split for time_trend)."
    )
    time_field: TimeField | None = Field(default=None, description="Date used for time_trend buckets.")
    time_granularity: TimeGranularity | None = None
    numeric_field: NumericField | None = Field(
        default=None, description="Field for histogram, or sort key for trial_list."
    )
    x_field: NumericField | None = Field(default=None, description="Scatter x axis.")
    y_field: NumericField | None = Field(default=None, description="Scatter y axis.")
    network_source: NetworkEntity | None = None
    network_target: NetworkEntity | None = Field(
        default=None, description="Same as source => co-occurrence network (e.g. drug-drug)."
    )
    filters: Filters = Field(default_factory=Filters)
    groups: list[Group] = Field(
        default_factory=list, description="2-6 groups for comparisons (e.g. drug A vs drug B); else empty."
    )
    metric: Metric = Field(default="count", description="'percent' normalises within each group.")
    top_n: int | None = Field(default=None, description="Max categories/nodes/rows to show.")
    chart_preference: ChartPreference = "auto"
    assumptions: list[str] = Field(default_factory=list, description="Assumptions made interpreting the question.")
