"""Planning stage: question (+ structured fields) -> validated, grounded :class:`QueryPlan`."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ..config import Settings
from ..schemas.plan import QueryPlan
from ..schemas.request import QueryRequest
from ..schemas.response import PlannerInfo
from .grounding import ground_plan
from .llm import LLMPlanError, LLMPlanner
from .rules import plan_with_rules
from .validate import PlanValidationError, apply_request_overrides, check_plan, normalize_plan

__all__ = ["Planner", "PlanOutcome", "PlanValidationError", "LLMPlanError"]


@dataclass
class PlanOutcome:
    plan: QueryPlan
    info: PlannerInfo
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


class Planner:
    def __init__(self, settings: Settings, llm_client: Any | None = None):
        self.settings = settings
        self.llm: LLMPlanner | None = None
        if llm_client is None and settings.openai_api_key:
            from openai import AsyncOpenAI

            llm_client = AsyncOpenAI(api_key=settings.openai_api_key)
        if llm_client is not None:
            self.llm = LLMPlanner(llm_client, settings.openai_model, settings.llm_timeout_s)

    async def plan(self, req: QueryRequest, today: date | None = None) -> PlanOutcome:
        today = today or date.today()
        mode = req.planner if req.planner != "auto" else self.settings.planner
        warnings: list[str] = []
        info: PlannerInfo
        raw: QueryPlan | None = None

        if mode in ("auto", "llm"):
            if self.llm is None:
                if mode == "llm":
                    raise LLMPlanError("planner='llm' requested but OPENAI_API_KEY is not configured")
                info = PlannerInfo(mode="rules", fallback_reason="no OPENAI_API_KEY configured")
            else:
                try:
                    raw, attempts = await self.llm.plan(req, today)
                    info = PlannerInfo(mode="llm", model=self.llm.model, attempts=attempts)
                except LLMPlanError as exc:
                    if mode == "llm":
                        raise
                    info = PlannerInfo(mode="rules", model=None, fallback_reason=str(exc))
                    warnings.append(f"LLM planner unavailable, used keyword planner instead ({exc}).")
        else:
            info = PlannerInfo(mode="rules")

        if raw is None:
            raw = plan_with_rules(req.query, today.year)

        plan, grounding_warnings = ground_plan(raw, req)
        plan, override_notes = apply_request_overrides(plan, req)
        plan, normalize_notes = normalize_plan(plan)
        check_plan(plan)  # overrides/normalisation must leave a valid plan; raises -> HTTP 422
        return PlanOutcome(plan, info, warnings + grounding_warnings, override_notes + normalize_notes)
