"""Evidence-carrying field extractors over raw ClinicalTrials.gov API v2 study records.

Every extractor returns not just a value but the *exact* JSON path(s) and raw
value(s) it was derived from. Aggregation keeps that evidence, which is what
makes deep citations possible: a bar labelled "Phase 3" can point at
``protocolSection.designModule.phases[0] == "PHASE3"`` in each trial that
contributed to it, and anyone can verify that against the API.

Records are deliberately *not* flattened into a lossy intermediate model first;
paths are relative to the original API JSON.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..schemas.enums import PHASE_LABELS, SPONSOR_CLASS_LABELS, enum_label

MISSING_LABEL = "Not specified"

PS = "protocolSection"
P_NCT = f"{PS}.identificationModule.nctId"
P_TITLE = f"{PS}.identificationModule.briefTitle"
P_OFFICIAL_TITLE = f"{PS}.identificationModule.officialTitle"
P_STATUS = f"{PS}.statusModule.overallStatus"
P_PHASES = f"{PS}.designModule.phases"
P_STUDY_TYPE = f"{PS}.designModule.studyType"
P_DESIGN_INFO = f"{PS}.designModule.designInfo"
P_ENROLLMENT = f"{PS}.designModule.enrollmentInfo"
P_INTERVENTIONS = f"{PS}.armsInterventionsModule.interventions"
P_CONDITIONS = f"{PS}.conditionsModule.conditions"
P_KEYWORDS = f"{PS}.conditionsModule.keywords"
P_LEAD = f"{PS}.sponsorCollaboratorsModule.leadSponsor"
P_COLLABS = f"{PS}.sponsorCollaboratorsModule.collaborators"
P_LOCATIONS = f"{PS}.contactsLocationsModule.locations"
P_OFFICIALS = f"{PS}.contactsLocationsModule.overallOfficials"
P_ELIG = f"{PS}.eligibilityModule"

TIME_PATHS = {
    "start_date": f"{PS}.statusModule.startDateStruct.date",
    "primary_completion_date": f"{PS}.statusModule.primaryCompletionDateStruct.date",
    "completion_date": f"{PS}.statusModule.completionDateStruct.date",
    "first_posted_date": f"{PS}.statusModule.studyFirstPostDateStruct.date",
}


@dataclass(frozen=True)
class Ev:
    """One piece of evidence: the value found at ``path`` in the API record."""

    path: str
    excerpt: str
    derived: bool = False


@dataclass
class Val:
    """A categorical value of a record plus the evidence supporting it."""

    key: str  # normalised grouping key
    label: str  # display form
    evidence: list[Ev] = field(default_factory=list)


# ---------------------------------------------------------------------- helpers
_PATH_TOKEN = re.compile(r"([^.\[\]]+)|\[(\d+)\]")


def get_path(obj: Any, path: str) -> Any:
    """Resolve ``a.b[0].c`` against nested dicts/lists; ``None`` if absent."""
    cur = obj
    for name, idx in _PATH_TOKEN.findall(path):
        if cur is None:
            return None
        if idx:
            i = int(idx)
            cur = cur[i] if isinstance(cur, list) and i < len(cur) else None
        else:
            cur = cur.get(name) if isinstance(cur, dict) else None
    return cur


def nct_id(study: dict[str, Any]) -> str:
    return get_path(study, P_NCT) or "UNKNOWN"


def brief_title(study: dict[str, Any]) -> str | None:
    return get_path(study, P_TITLE)


def year_of(date_str: Any) -> int | None:
    if isinstance(date_str, str) and len(date_str) >= 4 and date_str[:4].isdigit():
        return int(date_str[:4])
    return None


def parse_date(date_str: Any) -> date | None:
    """API dates are ``YYYY``, ``YYYY-MM`` or ``YYYY-MM-DD``; partial dates map to the 1st."""
    if not isinstance(date_str, str):
        return None
    parts = date_str.split("-")
    try:
        y = int(parts[0])
        m = int(parts[1]) if len(parts) > 1 else 1
        d = int(parts[2]) if len(parts) > 2 else 1
        return date(y, m, d)
    except (ValueError, IndexError):
        return None


def _list(study: dict[str, Any], path: str) -> list[Any]:
    value = get_path(study, path)
    return value if isinstance(value, list) else []


def _single(study: dict[str, Any], path: str, labeler: Callable[[str], str] = enum_label) -> list[Val]:
    raw = get_path(study, path)
    if raw is None or raw == "":
        return [Val(MISSING_LABEL.lower(), MISSING_LABEL, [Ev(path, "(field absent)", derived=True)])]
    raw_s = str(raw)
    return [Val(raw_s.lower(), labeler(raw_s), [Ev(path, raw_s)])]


def _each(
    study: dict[str, Any],
    path: str,
    attr: str | None,
    normalize: Callable[[str], Iterable[tuple[str, str]]] | None = None,
    keep: Callable[[dict[str, Any]], bool] | None = None,
) -> list[Val]:
    """One Val per distinct normalised value in a list field (a trial counts once per value)."""
    out: dict[str, Val] = {}
    for i, item in enumerate(_list(study, path)):
        if keep and isinstance(item, dict) and not keep(item):
            continue
        raw = item.get(attr) if (attr and isinstance(item, dict)) else item
        if not isinstance(raw, str) or not raw.strip():
            continue
        item_path = f"{path}[{i}].{attr}" if attr else f"{path}[{i}]"
        pairs = normalize(raw) if normalize else [(raw.strip().lower(), raw.strip())]
        for key, label in pairs:
            if key in out:
                # same value repeated (e.g. 40 sites in one country): the first occurrence is enough evidence
                if all(e.excerpt != raw for e in out[key].evidence):
                    out[key].evidence.append(Ev(item_path, raw))
            else:
                out[key] = Val(key, label, [Ev(item_path, raw)])
    return list(out.values())


# ------------------------------------------------------------------ normalisers
_PARENS = re.compile(r"\s*[\(\[][^)\]]*[\)\]]")
_DOSE = re.compile(
    r"\s*\b\d+(?:[.,]\d+)?\s*(?:mg/kg|mg/m2|mg/m²|mg|mcg|µg|ug|g|ml|iu|units?|%)(?![a-z]).*$", re.I
)
_FORM = re.compile(
    r"\b(?:injection|injectable|tablets?|capsules?|infusion|oral|intravenous|i\.v\.|iv|subcutaneous|"
    r"s\.c\.|solution|for injection|cream|gel)\b",
    re.I,
)
NON_DRUG = re.compile(
    r"\b(?:placebos?|saline|vehicle|sham|standard of care|best supportive care|usual care|no intervention)\b",
    re.I,
)
_SPLIT_COMBO = re.compile(r"\s+(?:\+|plus|and|with)\s+|\s*\+\s*", re.I)


def drug_names(raw: str) -> list[tuple[str, str]]:
    """``"Pembrolizumab 200 mg IV + Lenvatinib (E7080)"`` -> pembrolizumab, lenvatinib.

    Removes parentheticals, dose/route/formulation tokens and placebo-type
    controls; splits explicit combinations. Synonyms (e.g. MK-3475) are *not*
    merged (no drug dictionary is used) - a documented limitation.
    """
    out = []
    for part in _SPLIT_COMBO.split(raw):
        s = _PARENS.sub("", part)
        s = _DOSE.sub("", s)
        s = re.sub(r"\s+AUC\s*\d.*$", "", s, flags=re.I)
        s = _FORM.sub("", s)
        s = re.sub(r"\s{2,}", " ", s).strip(" ,;:-/")
        if len(s) < 2 or NON_DRUG.search(s):
            continue
        label = s if not s.islower() else s.capitalize()
        out.append((s.lower(), label))
    return out


def _person(raw: str) -> list[tuple[str, str]]:
    name = raw.split(",")[0].strip()
    name = re.sub(r"\b(?:MD|PhD|M\.D\.|Ph\.D\.|Dr\.?|Prof\.?)\b", "", name).strip()
    return [(name.lower(), name)] if name else []


def _plain(raw: str) -> list[tuple[str, str]]:
    s = raw.strip()
    return [(s.lower(), s)]


def phase_label(phases: list[str]) -> str:
    if not phases:
        return MISSING_LABEL
    labels = [PHASE_LABELS.get(p, enum_label(p)) for p in phases]
    if len(labels) == 2 and all(lbl.startswith("Phase ") for lbl in labels):
        return f"{labels[0]}/{labels[1].removeprefix('Phase ')}"  # "Phase 1/2"
    return "/".join(labels)


PHASE_ORDER = [
    "Early Phase 1", "Phase 1", "Phase 1/2", "Phase 2", "Phase 2/3", "Phase 3", "Phase 4",
    "Not Applicable", MISSING_LABEL,
]
AGE_LABELS = {"CHILD": "Child (0-17)", "ADULT": "Adult (18-64)", "OLDER_ADULT": "Older adult (65+)"}


# ------------------------------------------------------------------ dimensions
def _phase(study: dict[str, Any]) -> list[Val]:
    phases = _list(study, P_PHASES)
    label = phase_label(phases)
    if not phases:
        return [Val(label.lower(), label, [Ev(P_PHASES, "(field absent)", derived=True)])]
    return [Val(label.lower(), label, [Ev(f"{P_PHASES}[{i}]", p) for i, p in enumerate(phases)])]


def _sponsor_class(study: dict[str, Any]) -> list[Val]:
    return _single(study, f"{P_LEAD}.class", lambda v: SPONSOR_CLASS_LABELS.get(v, enum_label(v)))


def _has_results(study: dict[str, Any]) -> list[Val]:
    raw = study.get("hasResults")
    label = "Results posted" if raw else "No results posted"
    return [Val(label.lower(), label, [Ev("hasResults", str(bool(raw)).lower())])]


def _year_dim(path: str) -> Callable[[dict[str, Any]], list[Val]]:
    def extract(study: dict[str, Any]) -> list[Val]:
        raw = get_path(study, path)
        y = year_of(raw)
        if y is None:
            return []
        return [Val(str(y), str(y), [Ev(path, str(raw))])]

    return extract


def _age_groups(study: dict[str, Any]) -> list[Val]:
    return _each(study, f"{P_ELIG}.stdAges", None, lambda r: [(r.lower(), AGE_LABELS.get(r, enum_label(r)))])


def _facility(study: dict[str, Any]) -> list[Val]:
    out: dict[str, Val] = {}
    for i, loc in enumerate(_list(study, P_LOCATIONS)):
        fac = loc.get("facility")
        if not fac:
            continue
        city = loc.get("city") or ""
        key = f"{fac.lower()}|{city.lower()}"
        label = f"{fac}, {city}" if city else fac
        out.setdefault(key, Val(key, label, [Ev(f"{P_LOCATIONS}[{i}].facility", fac)]))
    return list(out.values())


@dataclass(frozen=True)
class DimensionSpec:
    name: str
    title: str
    extract: Callable[[dict[str, Any]], list[Val]]
    ordinal: bool = False
    order: tuple[str, ...] | None = None
    multi_valued: bool = False


def _dim(name, title, fn, **kw) -> tuple[str, DimensionSpec]:
    return name, DimensionSpec(name, title, fn, **kw)


DIMENSIONS: dict[str, DimensionSpec] = dict(
    [
        _dim("phase", "Phase", _phase, ordinal=True, order=tuple(PHASE_ORDER)),
        _dim("overall_status", "Overall status", lambda s: _single(s, P_STATUS)),
        _dim("study_type", "Study type", lambda s: _single(s, P_STUDY_TYPE)),
        _dim(
            "intervention_type",
            "Intervention type",
            lambda s: _each(s, P_INTERVENTIONS, "type", lambda r: [(r.lower(), enum_label(r))]),
            multi_valued=True,
        ),
        _dim(
            "drug",
            "Drug / biological",
            lambda s: _each(
                s, P_INTERVENTIONS, "name", drug_names, keep=lambda i: i.get("type") in ("DRUG", "BIOLOGICAL")
            ),
            multi_valued=True,
        ),
        _dim("intervention", "Intervention", lambda s: _each(s, P_INTERVENTIONS, "name", _plain), multi_valued=True),
        _dim("condition", "Condition", lambda s: _each(s, P_CONDITIONS, None, _plain), multi_valued=True),
        _dim("sponsor", "Lead sponsor", lambda s: _single(s, f"{P_LEAD}.name", str)),
        _dim("sponsor_class", "Lead sponsor class", _sponsor_class),
        _dim("collaborator", "Collaborator", lambda s: _each(s, P_COLLABS, "name", _plain), multi_valued=True),
        _dim("country", "Country", lambda s: _each(s, P_LOCATIONS, "country", _plain), multi_valued=True),
        _dim("facility", "Site (facility)", _facility, multi_valued=True),
        _dim("investigator", "Investigator", lambda s: _each(s, P_OFFICIALS, "name", _person), multi_valued=True),
        _dim("allocation", "Allocation", lambda s: _single(s, f"{P_DESIGN_INFO}.allocation")),
        _dim("intervention_model", "Intervention model", lambda s: _single(s, f"{P_DESIGN_INFO}.interventionModel")),
        _dim("primary_purpose", "Primary purpose", lambda s: _single(s, f"{P_DESIGN_INFO}.primaryPurpose")),
        _dim("masking", "Masking", lambda s: _single(s, f"{P_DESIGN_INFO}.maskingInfo.masking")),
        _dim("sex", "Sex eligibility", lambda s: _single(s, f"{P_ELIG}.sex")),
        _dim("age_group", "Age group", _age_groups, ordinal=True, order=tuple(AGE_LABELS.values()), multi_valued=True),
        _dim("has_results", "Results posted", _has_results),
        _dim("start_year", "Start year", _year_dim(TIME_PATHS["start_date"]), ordinal=True),
        _dim("completion_year", "Completion year", _year_dim(TIME_PATHS["completion_date"]), ordinal=True),
    ]
)


# ------------------------------------------------------------------ time buckets
def time_bucket(study: dict[str, Any], time_field: str, granularity: str) -> tuple[str, Ev] | None:
    path = TIME_PATHS[time_field]
    raw = get_path(study, path)
    if not isinstance(raw, str) or year_of(raw) is None:
        return None
    parts = raw.split("-")
    if granularity == "year":
        key = parts[0]
    elif len(parts) < 2:
        return None  # year-only date cannot be placed in a month/quarter
    elif granularity == "quarter":
        key = f"{parts[0]}-Q{(int(parts[1]) - 1) // 3 + 1}"
    else:
        key = f"{parts[0]}-{parts[1]}"
    return key, Ev(path, raw)


def bucket_range(first: str, last: str, granularity: str) -> list[str]:
    """All bucket keys between two keys (inclusive) so time series have explicit zeros."""
    if granularity == "year":
        return [str(y) for y in range(int(first), int(last) + 1)]
    if granularity == "quarter":
        (y0, q0), (y1, q1) = ((int(k[:4]), int(k[-1])) for k in (first, last))
        out, y, q = [], y0, q0
        while (y, q) <= (y1, q1):
            out.append(f"{y}-Q{q}")
            y, q = (y + 1, 1) if q == 4 else (y, q + 1)
        return out
    (y0, m0), (y1, m1) = ((int(k[:4]), int(k[5:7])) for k in (first, last))
    out, y, m = [], y0, m0
    while (y, m) <= (y1, m1):
        out.append(f"{y}-{m:02d}")
        y, m = (y + 1, 1) if m == 12 else (y, m + 1)
    return out


# ------------------------------------------------------------------ numeric fields
NUMERIC_META = {
    "enrollment": ("Enrollment (participants)", "participants"),
    "duration_months": ("Planned/actual duration (months)", "months"),
    "num_sites": ("Number of sites", "sites"),
    "num_countries": ("Number of countries", "countries"),
    "num_interventions": ("Number of interventions", "interventions"),
    "start_date": ("Start date", "date"),
}


def numeric(study: dict[str, Any], name: str) -> tuple[float | str, list[Ev]] | None:
    """Value of a numeric (or temporal, for ``start_date``) attribute with evidence."""
    if name == "enrollment":
        count = get_path(study, f"{P_ENROLLMENT}.count")
        if not isinstance(count, (int, float)):
            return None
        ev = [Ev(f"{P_ENROLLMENT}.count", str(count))]
        etype = get_path(study, f"{P_ENROLLMENT}.type")
        if etype:
            ev.append(Ev(f"{P_ENROLLMENT}.type", etype))
        return float(count), ev
    if name == "duration_months":
        sp = TIME_PATHS["start_date"]
        start = get_path(study, sp)
        end_path = TIME_PATHS["primary_completion_date"]
        end = get_path(study, end_path)
        if not end:
            end_path = TIME_PATHS["completion_date"]
            end = get_path(study, end_path)
        d0, d1 = parse_date(start), parse_date(end)
        if not d0 or not d1 or d1 < d0:
            return None
        months = round((d1 - d0).days / 30.4375, 1)
        return months, [Ev(sp, start, derived=True), Ev(end_path, end, derived=True)]
    if name == "num_sites":
        locs = _list(study, P_LOCATIONS)
        return float(len(locs)), [Ev(P_LOCATIONS, f"{len(locs)} location entries", derived=True)]
    if name == "num_countries":
        countries = sorted({loc.get("country") for loc in _list(study, P_LOCATIONS) if loc.get("country")})
        return float(len(countries)), [Ev(f"{P_LOCATIONS}[*].country", "; ".join(countries) or "(none)", derived=True)]
    if name == "num_interventions":
        names = [i.get("name", "") for i in _list(study, P_INTERVENTIONS)]
        return float(len(names)), [Ev(f"{P_INTERVENTIONS}[*].name", "; ".join(names) or "(none)", derived=True)]
    if name == "start_date":
        raw = get_path(study, TIME_PATHS["start_date"])
        if parse_date(raw) is None:
            return None
        return raw, [Ev(TIME_PATHS["start_date"], raw)]
    raise KeyError(name)


# ------------------------------------------------------------------ match evidence
_MATCH_PATHS = {
    "intervention": [(P_INTERVENTIONS, "name"), (P_INTERVENTIONS, "otherNames"), (P_TITLE, None)],
    "condition": [(P_CONDITIONS, None), (P_KEYWORDS, None), (P_TITLE, None)],
    "sponsor": [(f"{P_LEAD}.name", None), (P_COLLABS, "name")],
    "country": [(P_LOCATIONS, "country")],
    "term": [(P_TITLE, None), (P_OFFICIAL_TITLE, None), (P_CONDITIONS, None), (P_INTERVENTIONS, "name")],
}


def _walk_strings(study: dict[str, Any], path: str, attr: str | None) -> Iterable[tuple[str, str]]:
    value = get_path(study, path)
    if isinstance(value, str):
        yield path, value
        return
    if not isinstance(value, list):
        return
    for i, item in enumerate(value):
        target = item.get(attr) if (attr and isinstance(item, dict)) else item
        base = f"{path}[{i}].{attr}" if attr else f"{path}[{i}]"
        if isinstance(target, str):
            yield base, target
        elif isinstance(target, list):
            for j, t in enumerate(target):
                if isinstance(t, str):
                    yield f"{base}[{j}]", t


def match_evidence(study: dict[str, Any], text_filters: dict[str, str]) -> list[Ev]:
    """Where each text filter literally occurs in the record (first hit per filter)."""
    found: list[Ev] = []
    for fname, needle in text_filters.items():
        n = needle.lower()
        for path, attr in _MATCH_PATHS.get(fname, []):
            hit = next((Ev(p, s) for p, s in _walk_strings(study, path, attr) if n in s.lower()), None)
            if hit:
                found.append(hit)
                break
    return found
