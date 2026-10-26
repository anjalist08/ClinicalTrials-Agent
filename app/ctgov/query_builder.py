"""Translate plan :class:`Filters` into ClinicalTrials.gov API v2 query parameters.

Design choice: text filters go to the API's *search areas* (``query.cond``,
``query.intr`` ...) rather than being matched locally, because the API expands
synonyms and MeSH terms (e.g. ``NSCLC`` -> non-small cell lung cancer, ``MK-3475``
-> pembrolizumab). Enum filters are sent both as API filters *and* re-checked
locally on every record (:func:`record_matches`), so a malformed or ignored
server-side filter can never leak non-matching trials into a chart.
"""

from __future__ import annotations

from typing import Any

from ..schemas.plan import Filters
from . import extract as ex

# Data needed by the extractors. Requesting only these modules keeps pages small
# (full records include long free-text descriptions and results sections).
FIELDS = [
    "IdentificationModule",
    "StatusModule",
    "SponsorCollaboratorsModule",
    "ConditionsModule",
    "DesignModule",
    "ArmsInterventionsModule",
    "EligibilityModule",
    "ContactsLocationsModule",
    "HasResults",
]


def essie_clauses(f: Filters) -> list[str]:
    """Essie expressions (``filter.advanced``) for enum / range constraints."""
    clauses: list[str] = []
    if f.phases:
        clauses.append("AREA[Phase](" + " OR ".join(f.phases) + ")")
    if f.study_type:
        clauses.append(f"AREA[StudyType]{f.study_type}")
    if f.intervention_types:
        clauses.append("AREA[InterventionType](" + " OR ".join(f.intervention_types) + ")")
    if f.sponsor_classes:
        clauses.append("AREA[LeadSponsorClass](" + " OR ".join(f.sponsor_classes) + ")")
    if f.start_year_from or f.start_year_to:
        lo = f"{f.start_year_from}-01-01" if f.start_year_from else "MIN"
        hi = f"{f.start_year_to}-12-31" if f.start_year_to else "MAX"
        clauses.append(f"AREA[StartDate]RANGE[{lo},{hi}]")
    return clauses


def build_params(f: Filters, *, use_advanced: bool = True) -> dict[str, Any]:
    params: dict[str, Any] = {}
    if f.condition:
        params["query.cond"] = f.condition
    if f.intervention:
        params["query.intr"] = f.intervention
    if f.sponsor:
        params["query.spons"] = f.sponsor
    if f.term:
        params["query.term"] = f.term
    if f.country:
        params["query.locn"] = f.country
    if f.statuses:
        params["filter.overallStatus"] = ",".join(f.statuses)
    if use_advanced:
        clauses = essie_clauses(f)
        if clauses:
            params["filter.advanced"] = " AND ".join(clauses)
    return params


def merge_filters(base: Filters, override: Filters) -> Filters:
    """Layer ``override`` (e.g. a comparison group) on top of ``base``."""
    merged = base.model_dump()
    for key, value in override.model_dump().items():
        if value not in (None, [], ""):
            merged[key] = value
    return Filters.model_validate(merged)


def record_matches(study: dict[str, Any], f: Filters) -> bool:
    """Local verification of the enum/range filters (text filters are trusted to the API)."""
    if f.phases:
        phases = ex.get_path(study, "protocolSection.designModule.phases") or []
        if not set(phases) & set(f.phases):
            return False
    if f.statuses:
        if ex.get_path(study, "protocolSection.statusModule.overallStatus") not in f.statuses:
            return False
    if f.study_type:
        if ex.get_path(study, "protocolSection.designModule.studyType") != f.study_type:
            return False
    if f.intervention_types:
        interventions = ex.get_path(study, "protocolSection.armsInterventionsModule.interventions") or []
        if not {i.get("type") for i in interventions} & set(f.intervention_types):
            return False
    if f.sponsor_classes:
        cls = ex.get_path(study, "protocolSection.sponsorCollaboratorsModule.leadSponsor.class")
        if cls not in f.sponsor_classes:
            return False
    if f.start_year_from or f.start_year_to:
        year = ex.year_of(ex.get_path(study, "protocolSection.statusModule.startDateStruct.date"))
        if year is None:
            return False
        if f.start_year_from and year < f.start_year_from:
            return False
        if f.start_year_to and year > f.start_year_to:
            return False
    return True
