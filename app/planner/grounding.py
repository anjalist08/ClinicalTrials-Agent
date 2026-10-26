"""Grounding checks: every filter in a plan must be traceable to the user's input.

The most common LLM planning failure is not a malformed plan but a *plausible
extra constraint*: adding ``status=RECRUITING`` or a year range the user never
mentioned, or "correcting" a drug name into a different drug. These silently
change the answer. So after the model returns a plan:

* free-text filter values (drug, condition, sponsor, term, country) must occur in
  the query or the structured request fields (exact, token-wise or fuzzy match);
* enum filters (phases, statuses, years, ...) require a lexical cue in the query.

Anything ungrounded is removed and reported in ``meta.warnings``. Values that
came from structured request fields are always grounded.
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

from ..schemas.plan import Filters, QueryPlan
from ..schemas.request import QueryRequest
from ..viz.geo import iso_codes

_COUNTRY_ALIASES = {
    "us": "United States", "u s": "United States", "usa": "United States", "america": "United States",
    "uk": "United Kingdom", "britain": "United Kingdom", "england": "United Kingdom",
}

ENUM_CUES: dict[str, re.Pattern[str]] = {
    "phases": re.compile(r"\bphases?\b|\bph\s?[1-4i]|\bearly[- ]phase", re.I),
    "statuses": re.compile(
        r"recruit|enroll|complet|active|ongoing|terminat|withdraw|suspend|stopp|halt|status|open|closed|finish|unknown|not yet",
        re.I,
    ),
    "study_type": re.compile(r"interventional|observational|expanded access|registry|cohort", re.I),
    "sponsor_classes": re.compile(r"industry|pharma|compan|nih|government|federal|academic|universit|network|individual|non-profit|sponsor (class|type|categor)", re.I),
    "years": re.compile(r"\b(19|20)\d{2}\b|last|past|recent|since|decade|years?\b|before|after", re.I),
}


# Each intervention type needs its own cue: "which drugs co-occur in combination studies" must not ground
# COMBINATION_PRODUCT (a regulatory product category), only an explicit "combination product" does.
INTERVENTION_TYPE_CUES: dict[str, re.Pattern[str]] = {
    "DRUG": re.compile(r"\bdrugs?\b|medication|pharmacolog", re.I),
    "BIOLOGICAL": re.compile(r"biolog|vaccin|antibod|cell therap|car-?t\b", re.I),
    "DEVICE": re.compile(r"device", re.I),
    "BEHAVIORAL": re.compile(r"behavio|lifestyle|exercise|counsel", re.I),
    "PROCEDURE": re.compile(r"procedur|surg", re.I),
    "RADIATION": re.compile(r"radiat|radiother", re.I),
    "GENETIC": re.compile(r"gene\b|genetic|gene therap", re.I),
    "DIETARY_SUPPLEMENT": re.compile(r"dietary|supplement|nutrition|vitamin", re.I),
    "DIAGNOSTIC_TEST": re.compile(r"diagnostic", re.I),
    "COMBINATION_PRODUCT": re.compile(r"combination products?", re.I),
    "OTHER": re.compile(r"\bother intervention", re.I),
}

# Words that describe the question rather than name an entity; never valid as a search filter value.
GENERIC_VALUES = {
    "combination", "combinations", "combination therapy", "combination studies", "drug", "drugs", "trial",
    "trials", "study", "studies", "treatment", "treatments", "therapy", "therapies", "intervention",
    "interventions", "clinical trials", "sponsor", "sponsors", "condition", "conditions", "disease", "diseases",
}


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", s.lower()).strip()


def is_grounded(value: str, sources: list[str]) -> bool:
    # boolean search expressions ("A AND (B C)") are grounded when every operand is
    parts = [p.strip("() ") for p in re.split(r"\s+(?:AND|OR)\s+", value)]
    if len(parts) > 1:
        return all(is_grounded(p, sources) for p in parts if p)
    v = _norm(value)
    if not v:
        return False
    v_tokens = v.split()
    for src in sources:
        s = _norm(src)
        if not s:
            continue
        if v in s:
            return True
        s_tokens = s.split()
        if all(t in s_tokens for t in v_tokens):
            return True
        # fuzzy: compare against every window of the same length (typos, plurals)
        n = len(v_tokens)
        for i in range(max(1, len(s_tokens) - n + 1)):
            window = " ".join(s_tokens[i : i + n])
            if SequenceMatcher(None, v, window).ratio() >= 0.85:
                return True
    return False


def _country_grounded(value: str, sources: list[str]) -> bool:
    if is_grounded(value, sources):
        return True
    target = iso_codes(value)[0]
    if not target:
        return False
    for src in sources:
        tokens = _norm(src).split()
        for n in (1, 2, 3):
            for i in range(len(tokens) - n + 1):
                gram = " ".join(tokens[i : i + n])
                name = _COUNTRY_ALIASES.get(gram, gram)
                if len(name) > 3 and iso_codes(name)[0] == target:
                    return True
                if gram in _COUNTRY_ALIASES and iso_codes(_COUNTRY_ALIASES[gram])[0] == target:
                    return True
    return False


def _request_sources(req: QueryRequest) -> list[str]:
    vals = [req.query, req.drug_name, req.condition, req.sponsor, req.country, req.keyword]
    if req.compare:
        vals += req.compare.values
    return [v for v in vals if v]


def _clean_filters(
    f: Filters, sources: list[str], query: str, where: str, warnings: list[str], req: QueryRequest
) -> tuple[Filters, int]:
    data = f.model_dump()
    dropped = 0
    for key, value in f.text_items().items():
        if _norm(value) in GENERIC_VALUES:
            warnings.append(f"Dropped {where}filter {key}={value!r}: a generic word, not an entity to search for.")
            data[key] = None
            dropped += 1
            continue
        ok = _country_grounded(value, sources) if key == "country" else is_grounded(value, sources)
        if not ok:
            warnings.append(f"Dropped {where}filter {key}={value!r}: it does not appear in the question.")
            data[key] = None
            dropped += 1
    # values supplied by structured request fields are grounded by definition (the request re-applies them anyway)
    from_request = {
        "phases": set(req.trial_phase or []),
        "statuses": set(req.overall_status or []),
        "intervention_types": set(req.intervention_type or []),
        "sponsor_classes": set(req.sponsor_class or []),
    }
    if data["intervention_types"]:
        kept = [t for t in data["intervention_types"] if t in from_request["intervention_types"] or INTERVENTION_TYPE_CUES[t].search(query)]
        if kept != data["intervention_types"]:
            removed = [t for t in data["intervention_types"] if t not in kept]
            warnings.append(f"Dropped {where}filter intervention_types={removed}: the question does not mention them.")
            data["intervention_types"] = kept
            dropped += 1
    for key in ("phases", "statuses", "sponsor_classes"):
        if data[key] and set(data[key]) <= from_request[key]:
            continue
        if data[key] and not ENUM_CUES[key].search(query):
            warnings.append(f"Dropped {where}filter {key}={data[key]}: the question does not mention it.")
            data[key] = []
            dropped += 1
    if data["study_type"] and data["study_type"] != req.study_type and not ENUM_CUES["study_type"].search(query):
        warnings.append(f"Dropped {where}filter study_type={data['study_type']}: the question does not mention it.")
        data["study_type"] = None
        dropped += 1
    years_from_request = (data["start_year_from"], data["start_year_to"]) == (req.start_year, req.end_year)
    if (data["start_year_from"] or data["start_year_to"]) and not years_from_request and not ENUM_CUES["years"].search(query):
        warnings.append(f"Dropped {where}start-year filter: the question does not mention a time range.")
        data["start_year_from"] = data["start_year_to"] = None
        dropped += 1
    return Filters.model_validate(data), dropped


def ground_plan(plan: QueryPlan, req: QueryRequest) -> tuple[QueryPlan, list[str]]:
    """Remove ungrounded constraints. Run *before* request overrides are applied."""
    warnings: list[str] = []
    sources = _request_sources(req)
    filters, _ = _clean_filters(plan.filters, sources, req.query, "", warnings, req)
    groups = []
    for g in plan.groups:
        gf, dropped = _clean_filters(g.filters, sources, req.query, f"group '{g.label}' ", warnings, req)
        if dropped or gf.is_empty():
            continue
        groups.append(g.model_copy(update={"filters": gf}))
    if plan.groups and len(groups) < 2:
        warnings.append("Comparison removed: fewer than two groups could be grounded in the question.")
        groups = []
    return plan.model_copy(update={"filters": filters, "groups": groups}), warnings
