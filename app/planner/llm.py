"""LLM planner: natural language -> :class:`QueryPlan` via OpenAI Structured Outputs.

Guard-rails, in order:

1. **Constrained decoding** - the response must match the QueryPlan JSON schema
   (strict mode), so enums such as dimensions and phases cannot be invented.
2. **Pydantic validation** of the returned JSON.
3. **Semantic validation** (:func:`check_plan`); errors are sent back to the
   model for *one* repair attempt, then we give up and fall back to rules.
4. **Grounding** (done by the caller) strips filters not present in the input.

The model never sees or produces trial data.
"""

from __future__ import annotations

import json
import logging
from datetime import date
from typing import Any

from openai.lib._pydantic import to_strict_json_schema
from pydantic import ValidationError

from ..schemas.plan import QueryPlan
from ..schemas.request import QueryRequest
from .validate import PlanValidationError, check_plan

log = logging.getLogger(__name__)


class LLMPlanError(RuntimeError):
    pass


def _strip_defaults(node: Any) -> Any:
    """Strict mode rejects some keywords (e.g. ``default``); remove them recursively."""
    if isinstance(node, dict):
        return {k: _strip_defaults(v) for k, v in node.items() if k not in ("default", "examples")}
    if isinstance(node, list):
        return [_strip_defaults(v) for v in node]
    return node


PLAN_SCHEMA = _strip_defaults(to_strict_json_schema(QueryPlan))

SYSTEM_PROMPT = """You are the planning component of a ClinicalTrials.gov analytics service.
Convert the user's question into a QueryPlan (JSON, schema enforced). A deterministic engine then
fetches trials from the ClinicalTrials.gov API v2 and computes every number, so you NEVER output data,
counts, NCT IDs or guesses about results - only the plan.

ANALYSIS (pick exactly one):
- total: a single number ("how many trials ...?" with no breakdown).
- distribution: count trials per category of `dimension` ("by phase", "most common sponsors",
  "which countries have the most ..."). For geography use dimension=country; set chart_preference=map
  only if the user asks for a map/world view.
- cross_tab: two categorical dimensions ("phase by status", "intervention type broken down by phase").
- time_trend: counts per period ("per year", "over time", "trend", "since 2015", "each year").
  time_field defaults to start_date; use completion_date / first_posted_date only if asked.
  Optional series_dimension splits lines (e.g. "per year by phase").
- histogram: distribution of a numeric attribute (enrollment, duration_months, num_sites, ...).
- scatter: two numeric attributes per trial (x_field, y_field), e.g. "enrollment vs duration".
- network: relationships/co-occurrence between entities. network_source/network_target are entity
  types; equal types = co-occurrence (e.g. drug-drug "combination" questions), different = bipartite
  (e.g. sponsor-drug). Use for "network", "connections", "which X are studied together", "co-occur".
- trial_list: specific trials ("largest", "longest", "most recent", "list the trials"); numeric_field
  is the sort key (enrollment, duration_months, num_sites, start_date).

COMPARISONS: "A vs B", "compare A and B" -> `groups` with one entry per side (2-6). Put the varying
entity ONLY in each group's filters (e.g. groups[0].filters.intervention="nivolumab") and keep shared
constraints in the top-level filters. If the groups have very different sizes or the user asks for
shares/percentages, set metric=percent. Never create a single group.

FILTERS - strict rules:
- Copy entity text VERBATIM from the question (same spelling, abbreviations kept: "NSCLC" stays "NSCLC").
  The API expands synonyms itself. Never translate, correct or expand names, never add brand/generic names.
- Use `intervention` for drugs/biologics/devices/procedures, `condition` for diseases, `sponsor` for
  organisations, `country` for site locations, `term` for anything else (e.g. "mRNA", "CAR-T").
- Only add phases / statuses / years / study_type / intervention_types / sponsor_classes if the user
  explicitly mentions them. "recruiting" -> statuses=[RECRUITING]; "ongoing"/"active" ->
  [RECRUITING, ACTIVE_NOT_RECRUITING, ENROLLING_BY_INVITATION, NOT_YET_RECRUITING].
  Relative ranges ("last 5 years") are computed from today's date.
- If the user's structured fields (given below) cover an entity, refer to it implicitly - e.g. "this drug"
  - and do NOT duplicate it into filters; the service applies structured fields itself.
- A dimension word that defines the breakdown ("by phase") is NOT a filter.

Write `interpretation` as one plain sentence. List any judgement calls in `assumptions`.
If the question is ambiguous, prefer the simplest analysis that answers it. Leave unused fields null.

EXAMPLES
Q: "How has the number of trials for pembrolizumab changed per year since 2015?"
-> analysis=time_trend, time_field=start_date, time_granularity=year,
   filters={intervention:"pembrolizumab", start_year_from:2015}
Q: "Compare phases for trials involving nivolumab vs pembrolizumab"
-> analysis=distribution, dimension=phase, groups=[{label:"nivolumab",filters:{intervention:"nivolumab"}},
   {label:"pembrolizumab",filters:{intervention:"pembrolizumab"}}]
Q: "Which drugs frequently co-occur in combination studies for pancreatic cancer?"
-> analysis=network, network_source=drug, network_target=drug, filters={condition:"pancreatic cancer"}
Q: "Which countries have the most recruiting trials for multiple myeloma?"
-> analysis=distribution, dimension=country, filters={condition:"multiple myeloma", statuses:[RECRUITING]}
"""


def _user_message(req: QueryRequest, today: date) -> str:
    structured = req.model_dump(
        exclude_none=True,
        exclude={"query", "citation_mode", "max_citations_per_datum", "planner", "max_records", "top_n"},
    )
    return json.dumps({"today": today.isoformat(), "question": req.query, "structured_fields": structured}, default=str)


class LLMPlanner:
    def __init__(self, client: Any, model: str, timeout_s: float = 30.0, max_attempts: int = 2):
        self.client = client
        self.model = model
        self.timeout_s = timeout_s
        self.max_attempts = max_attempts

    def _kwargs(self) -> dict[str, Any]:
        kw: dict[str, Any] = {
            "model": self.model,
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "query_plan", "strict": True, "schema": PLAN_SCHEMA},
            },
            "timeout": self.timeout_s,
        }
        if not self.model.startswith("gpt-5"):  # reasoning models only accept the default temperature
            kw["temperature"] = 0
        return kw

    async def plan(self, req: QueryRequest, today: date | None = None) -> tuple[QueryPlan, int]:
        messages: list[dict[str, str]] = [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": _user_message(req, today or date.today())},
        ]
        last_error = "no attempt made"
        for attempt in range(1, self.max_attempts + 1):
            try:
                resp = await self.client.chat.completions.create(messages=messages, **self._kwargs())
            except Exception as exc:  # network, auth, rate limit ... -> caller falls back to rules
                raise LLMPlanError(f"LLM call failed: {type(exc).__name__}: {exc}") from exc
            choice = resp.choices[0]
            content = choice.message.content or ""
            if getattr(choice.message, "refusal", None):
                raise LLMPlanError(f"LLM refused: {choice.message.refusal}")
            try:
                plan = QueryPlan.model_validate_json(content)
                check_plan(plan)
                return plan, attempt
            except (ValidationError, PlanValidationError) as exc:
                last_error = str(exc)
                log.info("plan attempt %d invalid: %s", attempt, last_error)
                messages.append({"role": "assistant", "content": content})
                messages.append(
                    {
                        "role": "user",
                        "content": f"That plan is invalid: {last_error}. Return a corrected QueryPlan.",
                    }
                )
        raise LLMPlanError(f"LLM plan invalid after {self.max_attempts} attempts: {last_error}")
