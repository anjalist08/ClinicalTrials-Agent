"""Deterministic keyword planner.

Used when no LLM is configured, when the LLM call fails, or when forced with
``planner="rules"``. It is intentionally simple and transparent:

1. detect the analysis from cue phrases (network > scatter > histogram >
   time trend > trial list > total > cross-tab > distribution);
2. detect dimensions / entities / numeric fields from a lexicon;
3. pull out phases, statuses, years and countries with regexes;
4. whatever content words remain become a free-text ``term`` search (the API
   searches all fields and expands synonyms, so the planner does not need to
   know whether "pembrolizumab" is a drug or "melanoma" a condition);
5. ``A vs B`` / ``compare A and B`` become comparison groups.

It produces the same :class:`QueryPlan` as the LLM and goes through the same
validation, so the rest of the system cannot tell the difference.
"""

from __future__ import annotations

import re

import pycountry

from ..schemas.plan import Filters, Group, QueryPlan
from ..viz.geo import _ALIASES as COUNTRY_ALIASES

_SHORT_COUNTRIES = {"us": "United States", "usa": "United States", "u.s": "United States", "uk": "United Kingdom"}

# Ordered: longer / more specific phrases first.
DIMENSION_LEXICON: list[tuple[str, str]] = [
    (r"\bintervention (?:types?|categor\w*|modalit\w*)|\btypes? of interventions?|\bmodalit\w*", "intervention_type"),
    (r"\bsponsor (?:types?|class\w*|categor\w*)|\bfunder (?:types?|categor\w*)|\btypes? of sponsors?|\bindustry[- ](?:sponsored|funded)|\bindustry vs", "sponsor_class"),
    (r"\bstudy types?\b|\btypes? of stud\w*|\binterventional vs|\bobservational vs", "study_type"),
    (r"\bage groups?\b|\bages\b|\bpa?ediatric\b|\bchildren vs", "age_group"),
    (r"\bprimary purpose\b|\bpurposes?\b", "primary_purpose"),
    (r"\bintervention models?\b|\bdesign models?\b|\btrial designs?\b", "intervention_model"),
    (r"\ballocation\b|\brandomi[sz]\w*", "allocation"),
    (r"\bmasking\b|\bblind\w*", "masking"),
    (r"\bsex\b|\bgender\b", "sex"),
    (r"\bresults posted\b|\b(?:have|with|has) results\b", "has_results"),
    (r"\bstatu(?:s|ses)\b", "overall_status"),
    (r"\bphases?\b", "phase"),
    (r"\bcountr(?:y|ies)\b|\bgeograph\w*|\bwhere\b|\bregions?\b|\bnations?\b|\bmap\b|\bworld\w*|\bglobal(?:ly)?\b", "country"),
    (r"\bcollaborators?\b", "collaborator"),
    (r"\bsponsors?\b|\bcompan(?:y|ies)\b|\bpharma\w*|\bfunders?\b|\borgani[sz]ations?\b", "sponsor"),
    (r"\binvestigators?\b|\bpis?\b", "investigator"),
    (r"\bsites?\b|\bfacilit(?:y|ies)\b|\bhospitals?\b|\bcent(?:er|re)s?\b|\binstitutions?\b", "facility"),
    (r"\bconditions?\b|\bdiseases\b|\bindications?\b", "condition"),
    (r"\bdrugs?\b|\bmedications?\b|\btherap(?:y|ies)\b|\bcompounds?\b|\bagents?\b|\bbiologics?\b", "drug"),
    (r"\binterventions?\b|\btreatments?\b", "intervention"),
]

NUMERIC_LEXICON: list[tuple[str, str]] = [
    (r"\benrol+ment|\bparticipants\b|\bsample sizes?\b|\bpatients enrolled\b|\bnumber of patients\b|\btrial sizes?\b|\bhow (?:big|large)\b", "enrollment"),
    (r"\bduration\b|\bhow long\b|\blength of (?:the )?trials?\b", "duration_months"),
    (r"number of (?:sites|locations)|site counts?|how many sites", "num_sites"),
    (r"number of countries", "num_countries"),
    (r"number of interventions|how many (?:arms|interventions)", "num_interventions"),
    (r"start dates?|start years?|when (?:they|trials) started", "start_date"),
]

STATUS_LEXICON: list[tuple[str, list[str]]] = [
    (r"not yet recruiting", ["NOT_YET_RECRUITING"]),
    (r"actively recruiting|currently recruiting|\brecruiting\b|open for enrol+ment|enrolling", ["RECRUITING"]),
    (r"active,? not recruiting", ["ACTIVE_NOT_RECRUITING"]),
    (r"\bongoing\b|\bactive\b", ["RECRUITING", "ACTIVE_NOT_RECRUITING", "ENROLLING_BY_INVITATION", "NOT_YET_RECRUITING"]),
    (r"\bcompleted\b|\bfinished\b", ["COMPLETED"]),
    (r"\bterminated\b|\bstopped\b|\bhalted\b", ["TERMINATED"]),
    (r"\bwithdrawn\b", ["WITHDRAWN"]),
    (r"\bsuspended\b", ["SUSPENDED"]),
]

_ROMAN = {"i": "1", "ii": "2", "iii": "3", "iv": "4"}
PHASE_RE = re.compile(r"\b(early\s+)?phase\s*(1|2|3|4|iv|iii|ii|i)\b(?:\s*(?:/|and|or|-)\s*(?:phase\s*)?(1|2|3|4|iv|iii|ii|i)\b)?", re.I)
YEAR_RANGE_RE = re.compile(r"\b(?:between|from)\s+((?:19|20)\d{2})\s+(?:and|to|-)\s+((?:19|20)\d{2})\b", re.I)
SINCE_RE = re.compile(r"\b(?:since|after|from|starting(?: in)?)\s+((?:19|20)\d{2})\b", re.I)
BEFORE_RE = re.compile(r"\b(?:before|until|through|up to)\s+((?:19|20)\d{2})\b", re.I)
LAST_N_RE = re.compile(r"\b(?:last|past)\s+(\d{1,2})\s+years?\b", re.I)
DECADE_RE = re.compile(r"\b(?:last|past) decade\b", re.I)

STOPWORDS = set(
    """a an the of for in on at to by with without about across among between from into over per vs versus and or
    is are was were be been being has have had do does did can could should would will which what who whom whose how
    many much most more least less top number numbers count counts show shows display plot chart graph visualize
    visualise give list find me us our my trial trials study studies clinical research there their them this that
    these those it its each every year years yearly annually annual month months monthly quarter quarterly time over
    changed change changes changing trend trends evolve evolved grow growth distributed distribution distributions
    breakdown broken down split common frequent frequently typical commonly compare comparing comparison versus
    involving involve involved using use used test testing tested evaluating evaluate all any some new current
    currently recent recently network networks graph co occur occurrence occurrences cooccur relationship relationships
    connected connections link links between combination combinations combined together frequently largest biggest
    smallest longest shortest newest oldest highest lowest rank ranked ranking sorted than so far since before after
    until through up last past decade started start starting begun began run running conducted registered done
    histogram scatter correlation correlate correlated vs. see look looking like based as per by type types kind kinds
    category categories class classes size sizes where get across sponsor sponsors sponsored drug drugs condition
    conditions diseases phase phases status statuses country countries site sites intervention interventions investigator
    investigators collaborator collaborators enrollment enrolment participants patients duration length
    treatments treatment therapies therapy indication indications facility facilities hospital
    hospitals company companies pharma organization organizations organisations funder funders results posted
    allocation masking purpose design model age ages group groups sex gender recruiting recruit completed terminated
    withdrawn suspended ongoing active open enrolling invitation not yet early ph i ii iii iv medication medications
    agents agent biologic biologics compounds compound sample regions region geographic geography map world globally
    involve frequency often studied investigated researched two three four five several various different
    co-occur co-occurring cooccur cooccurring percentage percent proportion share fraction industry-sponsored
    industry-funded industry worldwide globally world vs/ ↔ <-> - — – / & + , ? . ! : ; ( )""".split()
)

CO_OCCUR_RE = re.compile(r"co-?occur|combination|together|paired|same trials?", re.I)


def _find_all(text: str, lexicon: list[tuple[str, str]]) -> list[tuple[int, str]]:
    """(position, label) for every lexicon hit, without double-counting overlapping phrases."""
    hits: list[tuple[int, str]] = []
    taken: list[tuple[int, int]] = []
    for pattern, label in lexicon:
        for m in re.finditer(pattern, text, re.I):
            if any(m.start() < e and m.end() > s for s, e in taken):
                continue
            taken.append((m.start(), m.end()))
            hits.append((m.start(), label))
    return sorted(hits)


def _unique(seq: list[str]) -> list[str]:
    return list(dict.fromkeys(seq))


def _residual_chunks(text: str) -> list[str]:
    """Contiguous runs of content words (not stopwords, numbers or cue words)."""
    tokens = re.findall(r"[A-Za-z0-9][\w\-'.]*[\w]|[A-Za-z0-9]|[↔/&+,?.!:;()]|<->|-", text)
    chunks, cur = [], []
    for tok in tokens:
        low = tok.lower().strip(".")
        if not low or low in STOPWORDS or re.fullmatch(r"(19|20)\d{2}|\d+", low):
            if cur:
                chunks.append(" ".join(cur))
                cur = []
        else:
            cur.append(tok.strip("."))
    if cur:
        chunks.append(" ".join(cur))
    return [c for c in chunks if len(c) > 1]


def _years(text: str, current_year: int) -> tuple[int | None, int | None, str]:
    if m := YEAR_RANGE_RE.search(text):
        return int(m.group(1)), int(m.group(2)), m.group(0)
    lo = hi = None
    spans = []
    if m := SINCE_RE.search(text):
        lo = int(m.group(1))
        spans.append(m.group(0))
    if m := BEFORE_RE.search(text):
        hi = int(m.group(1)) - (1 if m.group(0).lower().startswith("before") else 0)
        spans.append(m.group(0))
    if m := LAST_N_RE.search(text):
        lo = current_year - int(m.group(1))
        spans.append(m.group(0))
    elif m := DECADE_RE.search(text):
        lo = current_year - 10
        spans.append(m.group(0))
    return lo, hi, " ".join(spans)


def _phases(text: str) -> list[str]:
    out = []
    for m in PHASE_RE.finditer(text):
        nums = [n for n in (m.group(2), m.group(3)) if n]
        for n in nums:
            n = _ROMAN.get(n.lower(), n)
            out.append("EARLY_PHASE1" if (m.group(1) and n == "1") else f"PHASE{n}")
    return _unique(out)


def _statuses(text: str) -> list[str]:
    taken: list[tuple[int, int]] = []
    out: list[str] = []
    for pattern, vals in STATUS_LEXICON:
        for m in re.finditer(pattern, text, re.I):
            if any(m.start() < e and m.end() > s for s, e in taken):
                continue
            taken.append((m.start(), m.end()))
            out += vals
    return _unique(out)


def _country_of(chunk: str) -> str | None:
    """Exact country-name match (no fuzzy matching: 'Lung' must never become a country)."""
    low = chunk.lower()
    if low in _SHORT_COUNTRIES:
        return _SHORT_COUNTRIES[low]
    if len(chunk) < 4:
        return None
    if low in COUNTRY_ALIASES:
        return chunk
    try:
        pycountry.countries.lookup(chunk)
        return chunk
    except LookupError:
        return None


def _group_field(text: str) -> str:
    t = text.lower()
    if re.search(r"\bdrugs?\b|involving|medication|therap|treatment|intervention|\bagents?\b|vaccine", t):
        return "intervention"
    if re.search(r"\bconditions?\b|\bdiseases?\b|\bindications?\b", t):
        return "condition"
    return "term"


def plan_with_rules(query: str, current_year: int) -> QueryPlan:
    text = query.strip()
    low = text.lower()
    assumptions: list[str] = ["Planned by the deterministic keyword planner (no LLM)."]

    dims = _unique([d for _, d in _find_all(text, DIMENSION_LEXICON)])
    nums = _unique([n for _, n in _find_all(text, NUMERIC_LEXICON)])

    # ---- filters -------------------------------------------------------
    y_from, y_to, _ = _years(text, current_year)
    filters = Filters(
        phases=_phases(text) if "phase" not in dims or re.search(r"phase\s*\d|phase\s+i", low) else [],
        statuses=_statuses(text),
        start_year_from=y_from,
        start_year_to=y_to,
    )
    # "phase" as a dimension word ("by phase") should not also become a filter
    if filters.phases and re.search(r"(?:by|across|per|each|distribution of|compare) phases?\b", low):
        filters.phases = []

    # comparison groups: split on vs / versus, or "compare A and B"
    groups: list[Group] = []
    vs_parts = re.split(r"\s+(?:vs\.?|versus)\s+", text, flags=re.I)
    if len(vs_parts) == 1 and re.search(r"\bcompar\w*", low):
        vs_parts = re.split(r"\s+and\s+", text, maxsplit=1, flags=re.I)
    if len(vs_parts) >= 2:
        left = _residual_chunks(vs_parts[0])
        rights = [(_residual_chunks(p) or [""])[0] for p in vs_parts[1:]]
        labels = ([left[-1]] if left else []) + [r for r in rights if r]
        if len(labels) >= 2 and len({lbl.lower() for lbl in labels}) == len(labels):
            field = _group_field(text)
            if field == "term" and all(_country_of(lbl) for lbl in labels):
                field = "country"
            groups = [Group(label=lbl, filters=Filters(**{field: lbl})) for lbl in labels[:6]]
            assumptions.append(f"Compared groups by {field}: {', '.join(labels[:6])}.")

    used = {g.label.lower() for g in groups}
    chunks = [c for c in _residual_chunks(text) if c.lower() not in used]
    country = None
    terms = []
    for c in chunks:
        ctry = _country_of(c)
        if ctry and "country" not in dims:
            country = ctry
        elif ctry and country is None and re.search(rf"\bin\s+{re.escape(c)}", text, re.I):
            country = ctry
        else:
            terms.append(c)
    filters.country = country
    if terms:
        filters.term = " AND ".join(f"({t})" if " " in t else t for t in terms) if len(terms) > 1 else terms[0]
        assumptions.append(f"Searched all fields for: {filters.term}.")

    plan = dict(interpretation="", filters=filters, groups=groups, assumptions=assumptions)

    # ---- analysis ------------------------------------------------------
    network_cue = re.search(r"network|↔|<->|co-?occur|connect\w*|relationships? between|links? between|graph of", low)
    entity_dims = [d for d in dims if d in {"drug", "intervention", "condition", "sponsor", "collaborator", "investigator", "facility", "country", "intervention_type", "sponsor_class", "phase"}]
    if network_cue or (CO_OCCUR_RE.search(low) and "drug" in dims):
        src = entity_dims[0] if entity_dims else "drug"
        tgt = entity_dims[1] if len(entity_dims) > 1 else src
        plan.update(analysis="network", network_source=src, network_target=tgt)
        plan["interpretation"] = f"Network of {src} ↔ {tgt} co-occurrence in matching trials."
    elif re.search(r"scatter|correlat|relationship between|vs\.? (?:enrol|durat|start)", low) and len(nums) >= 2:
        plan.update(analysis="scatter", x_field=nums[1], y_field=nums[0])
        plan["interpretation"] = f"Scatter of {nums[0]} vs {nums[1]}, one point per trial."
    elif nums and nums[0] != "start_date" and re.search(r"histogram|distribution of|distributed|spread|how (?:big|large|long)|sizes?\b|typical", low) and not re.search(r"largest|biggest|longest|top \d+", low):
        plan.update(analysis="histogram", numeric_field=nums[0])
        plan["interpretation"] = f"Histogram of {nums[0]} across matching trials."
    elif re.search(r"over time|per (?:year|month|quarter)|each (?:year|month|quarter)|by (?:year|month|quarter)|yearly|annual|monthly|quarterly|trend|timeline|changed|evolv|growth|since (?:19|20)\d{2}|started each", low):
        gran = "month" if re.search(r"month", low) else "quarter" if re.search(r"quarter", low) else "year"
        time_field = "completion_date" if re.search(r"complet(?:ed|ion) (?:each|per|by)", low) else "start_date"
        series = next((d for d in dims if d not in {"drug", "intervention"} and d in {"phase", "overall_status", "sponsor_class", "intervention_type", "study_type"}), None) if not groups else None
        plan.update(analysis="time_trend", time_field=time_field, time_granularity=gran, series_dimension=series)
        plan["interpretation"] = f"Number of trials per {gran} by {time_field.replace('_', ' ')}."
    elif re.search(r"largest|biggest|longest|top \d+ trials|which trials|list (?:the )?trials|show (?:me )?(?:the )?trials|most recent trials|newest", low):
        nf = "duration_months" if "longest" in low else "start_date" if re.search(r"recent|newest", low) else (nums[0] if nums else "enrollment")
        m = re.search(r"top (\d+)", low)
        plan.update(analysis="trial_list", numeric_field=nf, top_n=int(m.group(1)) if m else None)
        plan["interpretation"] = f"Trials ranked by {nf}."
    elif re.search(r"^how many\b|total number|number of (?:matching )?trials\b", low) and not dims:
        plan.update(analysis="total")
        plan["interpretation"] = "Total number of matching trials."
    elif len(dims) >= 2 and not groups and re.search(r"\bby\b.*\b(?:and|by)\b|broken down|cross|split by|heatmap|\bby\b", low):
        plan.update(analysis="cross_tab", dimension=dims[0], series_dimension=dims[1])
        plan["interpretation"] = f"Trials by {dims[0]} broken down by {dims[1]}."
    else:
        dim = dims[0] if dims else "phase"
        if not dims:
            assumptions.append("No breakdown requested explicitly; defaulting to trials by phase.")
        plan.update(analysis="distribution", dimension=dim)
        plan["interpretation"] = f"Number of trials by {dim.replace('_', ' ')}."
        if dim == "country" and re.search(r"\bmap\b|world|globally|geograph", low):
            plan["chart_preference"] = "map"
    if groups:
        plan["interpretation"] += " Compared across: " + ", ".join(g.label for g in groups) + "."
    if re.search(r"percent|proportion|share|fraction|%", low):
        plan["metric"] = "percent"
    if re.search(r"heatmap", low):
        plan["chart_preference"] = "heatmap"
    elif re.search(r"stacked", low):
        plan["chart_preference"] = "stacked"
    if m := re.search(r"\btop (\d+)\b", low):
        plan.setdefault("top_n", int(m.group(1)))
        if plan.get("top_n") is None:
            plan["top_n"] = int(m.group(1))
    return QueryPlan.model_validate(plan)
