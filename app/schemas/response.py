"""Response schema for ``POST /v1/query``.

Contract for renderers (see ``docs/SCHEMA.md`` for the per-type tables):

* ``visualization.type`` selects the renderer.
* ``visualization.encoding`` maps *channel names* (``x``, ``y``, ``color``,
  ``node_size`` ...) to columns in the data. The set of channels used by each
  type is fixed and documented, so a renderer never has to guess.
* ``visualization.data`` is a list of flat row objects for every type except
  ``network_graph``, where it is ``{"nodes": [...], "edges": [...]}``.
* Every row / node / edge may carry ``citations`` (deep links to the trial
  records it was computed from) and ``citation_count`` (how many trials
  contributed, which is always the full number even when ``citations`` is
  truncated).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .enums import VizType
from .plan import Filters, QueryPlan


class Evidence(BaseModel):
    """A single field/value from the ClinicalTrials.gov API record."""

    path: str = Field(description="JSON path inside the API v2 study record, e.g. protocolSection.designModule.phases[0].")
    excerpt: str = Field(description="The exact value found at `path` (stringified if not a string).")
    derived: bool = Field(
        default=False,
        description="True when the datum is computed from the value (e.g. a count of locations) rather than equal to it.",
    )


class Citation(BaseModel):
    nct_id: str
    url: str = Field(description="Human-readable study page on clinicaltrials.gov.")
    api_url: str = Field(description="Raw API v2 record for machine verification.")
    brief_title: str | None = None
    evidence: list[Evidence] = Field(description="Field values that place this trial in this datum.")
    match_evidence: list[Evidence] = Field(
        default_factory=list,
        description="Where the search terms (e.g. the drug name) literally occur in the record. "
        "Empty when the API matched via a synonym.",
    )


class Channel(BaseModel):
    """Maps one visual channel to one data column."""

    model_config = ConfigDict(extra="forbid")

    field: str
    type: Literal["nominal", "ordinal", "quantitative", "temporal"]
    title: str
    sort: Literal["ascending", "descending", "data"] | list[str] | None = Field(
        default=None, description="'data' = keep row order; a list gives an explicit category order."
    )
    format: str | None = Field(default=None, description="d3-format / d3-time-format string.")
    unit: str | None = None
    time_unit: Literal["year", "quarter", "month"] | None = None
    scale: Literal["linear", "log", "sqrt", "symlog"] | None = None


class NetworkData(BaseModel):
    nodes: list[dict[str, Any]]
    edges: list[dict[str, Any]]


class Visualization(BaseModel):
    type: VizType
    title: str
    subtitle: str | None = None
    encoding: dict[str, Channel]
    data: list[dict[str, Any]] | NetworkData


class ApiRequestLog(BaseModel):
    group: str | None = None
    url: str
    total_count: int | None = Field(description="Matching trials reported by the API (countTotal).")
    retrieved: int
    pages: int
    from_cache: bool = False
    fallbacks: list[str] = Field(default_factory=list)


class DataCompleteness(BaseModel):
    total_matching: int | None
    records_analyzed: int
    complete: bool
    note: str | None = None


class PlannerInfo(BaseModel):
    mode: Literal["llm", "rules"]
    model: str | None = None
    attempts: int = 1
    fallback_reason: str | None = None


class SourceInfo(BaseModel):
    name: str = "ClinicalTrials.gov"
    api: str
    api_version: str | None = None
    data_timestamp: str | None = None
    retrieved_at: str


class Rendering(BaseModel):
    """Hints a renderer needs beyond the encoding."""

    orientation: Literal["vertical", "horizontal"] | None = None
    stacked: bool | None = None
    time_granularity: Literal["year", "quarter", "month"] | None = None
    sort: str | None = Field(default=None, description="How rows are ordered in `data`.")
    units: dict[str, str] = Field(default_factory=dict, description="Unit for each numeric column.")
    grouping: str | None = Field(default=None, description="What the colour/series channel represents.")
    value_field: str | None = Field(default=None, description="Primary numeric column.")
    categories_total: int | None = Field(default=None, description="Distinct categories/nodes before top_n truncation.")
    categories_shown: int | None = None
    missing_values: str | None = Field(default=None, description="How missing values are represented.")


class Meta(BaseModel):
    query: str
    interpretation: str
    visualization_rationale: str
    plan: QueryPlan
    planner: PlannerInfo
    filters_applied: Filters
    groups: list[dict[str, Any]] = Field(default_factory=list)
    counting_rule: str
    rendering: Rendering
    data_completeness: DataCompleteness
    citations: dict[str, Any] = Field(default_factory=dict, description="Citation mode and limits.")
    api_requests: list[ApiRequestLog]
    assumptions: list[str] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    source: SourceInfo
    timing_ms: dict[str, int] = Field(default_factory=dict)


class QueryResponse(BaseModel):
    visualization: Visualization
    meta: Meta


class ErrorResponse(BaseModel):
    error: str
    detail: Any = None
