"""Request schema for ``POST /v1/query``.

Only ``query`` is required. Every optional structured field is *authoritative*:
when present it overrides whatever the planner inferred from the free text, so a
frontend with form controls gets deterministic behaviour while a chat-style
client can send the question alone.
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .enums import (
    CitationMode,
    CompareField,
    InterventionType,
    OverallStatus,
    Phase,
    SponsorClass,
    StudyType,
    VizType,
)

_ROMAN = {"i": "1", "ii": "2", "iii": "3", "iv": "4"}


def normalize_phase(value: Any) -> str:
    """Accept the many ways people write a phase and return the API enum.

    ``"Phase 3"``, ``"phase III"``, ``"3"``, ``"PHASE3"`` -> ``"PHASE3"``;
    ``"early phase 1"`` -> ``"EARLY_PHASE1"``; ``"n/a"`` -> ``"NA"``.
    """
    if not isinstance(value, (str, int)):
        raise ValueError(f"invalid phase {value!r}")
    s = str(value).strip().lower().replace("_", " ").replace("-", " ")
    if s in {"na", "n/a", "not applicable"}:
        return "NA"
    early = "early" in s or s in {"0", "phase 0"}
    s = s.replace("early", "").replace("phase", "").strip()
    s = _ROMAN.get(s, s)
    if early and s in {"1", "", "0"}:
        return "EARLY_PHASE1"
    if s in {"1", "2", "3", "4"}:
        return f"PHASE{s}"
    raise ValueError(f"invalid phase {value!r}; expected e.g. 'Phase 2', 'PHASE3', 'early phase 1', 'NA'")


def normalize_enum(value: Any) -> str:
    """``"not yet recruiting"`` / ``"Not-Yet-Recruiting"`` -> ``"NOT_YET_RECRUITING"``."""
    if not isinstance(value, str):
        raise ValueError(f"expected a string, got {value!r}")
    return re.sub(r"[\s\-/]+", "_", value.strip()).upper()


class CompareSpec(BaseModel):
    """Explicit comparison: one dataset per value, rendered as series."""

    model_config = ConfigDict(extra="forbid")

    field: CompareField = Field(
        description="Which search area each value applies to. 'term' searches all text fields."
    )
    values: list[str] = Field(min_length=2, max_length=6, description="2-6 values to compare.")

    @field_validator("values")
    @classmethod
    def _clean_values(cls, v: list[str]) -> list[str]:
        cleaned = [s.strip() for s in v if s and s.strip()]
        if len({s.lower() for s in cleaned}) != len(cleaned) or len(cleaned) < 2:
            raise ValueError("compare.values must contain at least 2 distinct non-empty strings")
        return cleaned


class QueryRequest(BaseModel):
    """Natural-language question plus optional, authoritative structured filters."""

    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "query": "How has the number of trials for this drug changed over time?",
                    "drug_name": "Pembrolizumab",
                },
                {"query": "Compare phases for trials involving nivolumab vs pembrolizumab"},
                {
                    "query": "Show a network of sponsors and drugs",
                    "condition": "glioblastoma",
                    "trial_phase": ["Phase 2", "Phase 3"],
                },
            ]
        },
    )

    query: str = Field(min_length=3, max_length=1000, description="Natural-language question.")

    # ---- authoritative filters -------------------------------------------------
    drug_name: str | None = Field(
        default=None, max_length=200, description="Intervention/drug searched in the intervention area."
    )
    condition: str | None = Field(default=None, max_length=200, description="Condition/disease.")
    sponsor: str | None = Field(default=None, max_length=200, description="Lead sponsor or collaborator.")
    country: str | None = Field(default=None, max_length=100, description="Trial site country.")
    keyword: str | None = Field(default=None, max_length=200, description="Free-text term searched everywhere.")
    trial_phase: list[Phase] | None = Field(
        default=None, description="Phases; accepts 'Phase 3', 'phase III', '3', 'PHASE3', 'early phase 1', 'NA'."
    )
    overall_status: list[OverallStatus] | None = Field(
        default=None, description="Recruitment statuses, e.g. ['RECRUITING'] (case/spacing tolerant)."
    )
    study_type: StudyType | None = None
    intervention_type: list[InterventionType] | None = None
    sponsor_class: list[SponsorClass] | None = None
    start_year: int | None = Field(default=None, ge=1900, le=2100, description="Earliest study start year.")
    end_year: int | None = Field(default=None, ge=1900, le=2100, description="Latest study start year.")
    compare: CompareSpec | None = None

    # ---- output controls -------------------------------------------------------
    visualization_type: VizType | None = Field(
        default=None, description="Preferred chart type; honoured when compatible with the question."
    )
    top_n: int | None = Field(default=None, ge=1, le=100, description="Max categories / nodes / rows.")
    max_records: int | None = Field(
        default=None, ge=1, le=10000, description="Cap on trial records fetched per dataset."
    )
    citation_mode: CitationMode = Field(
        default="summary",
        description="'off': no citations; 'summary': up to max_citations_per_datum per datum; "
        "'full': every contributing trial is cited.",
    )
    max_citations_per_datum: int = Field(default=3, ge=1, le=50)
    planner: str = Field(
        default="auto", pattern="^(auto|llm|rules)$", description="Force a planner (debugging/evaluation)."
    )

    @field_validator("query", "drug_name", "condition", "sponsor", "country", "keyword", mode="before")
    @classmethod
    def _strip(cls, v: Any) -> Any:
        if isinstance(v, str):
            v = v.strip()
            return v or None
        return v

    @field_validator("trial_phase", mode="before")
    @classmethod
    def _phases(cls, v: Any) -> Any:
        if v is None:
            return v
        items = v if isinstance(v, list) else [v]
        return list(dict.fromkeys(normalize_phase(x) for x in items))

    @field_validator("overall_status", "intervention_type", "sponsor_class", mode="before")
    @classmethod
    def _enums(cls, v: Any) -> Any:
        if v is None:
            return v
        items = v if isinstance(v, list) else [v]
        return list(dict.fromkeys(normalize_enum(x) for x in items))

    @field_validator("study_type", mode="before")
    @classmethod
    def _study_type(cls, v: Any) -> Any:
        return normalize_enum(v) if isinstance(v, str) else v

    @model_validator(mode="after")
    def _check_years(self) -> QueryRequest:
        if self.start_year and self.end_year and self.start_year > self.end_year:
            raise ValueError("start_year must be <= end_year")
        return self
