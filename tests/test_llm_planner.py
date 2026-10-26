"""LLM planner behaviour with a scripted fake OpenAI client (no network)."""

import json
from datetime import date
from types import SimpleNamespace

import pytest

from app.config import Settings
from app.planner import LLMPlanError, Planner
from app.planner.llm import PLAN_SCHEMA, LLMPlanner
from app.schemas.request import QueryRequest


class FakeCompletions:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        content = reply if isinstance(reply, str) else json.dumps(reply)
        msg = SimpleNamespace(content=content, refusal=None)
        return SimpleNamespace(choices=[SimpleNamespace(message=msg)])


def fake_client(*replies):
    comp = FakeCompletions(replies)
    return SimpleNamespace(chat=SimpleNamespace(completions=comp)), comp


def plan_json(**kw):
    base = {
        "interpretation": "x", "analysis": "distribution", "dimension": "phase", "series_dimension": None,
        "time_field": None, "time_granularity": None, "numeric_field": None, "x_field": None, "y_field": None,
        "network_source": None, "network_target": None,
        "filters": {"condition": None, "intervention": None, "sponsor": None, "term": None, "country": None,
                    "phases": [], "statuses": [], "study_type": None, "intervention_types": [],
                    "sponsor_classes": [], "start_year_from": None, "start_year_to": None},
        "groups": [], "metric": "count", "top_n": None, "chart_preference": "auto", "assumptions": [],
    }
    for k, v in kw.items():
        if k == "filters":
            base["filters"].update(v)
        else:
            base[k] = v
    return base


def test_schema_is_strict_compatible():
    text = json.dumps(PLAN_SCHEMA)
    assert '"default"' not in text
    assert PLAN_SCHEMA["additionalProperties"] is False
    assert set(PLAN_SCHEMA["required"]) == set(PLAN_SCHEMA["properties"])


async def test_valid_plan_first_try_and_request_shape():
    client, comp = fake_client(plan_json(filters={"intervention": "pembrolizumab"}))
    planner = LLMPlanner(client, "gpt-4.1-mini")
    plan, attempts = await planner.plan(QueryRequest(query="pembrolizumab trials by phase"), date(2026, 1, 1))
    assert attempts == 1 and plan.filters.intervention == "pembrolizumab"
    call = comp.calls[0]
    assert call["response_format"]["json_schema"]["strict"] is True
    assert call["temperature"] == 0
    user = json.loads(call["messages"][1]["content"])
    assert user["today"] == "2026-01-01" and user["question"] == "pembrolizumab trials by phase"


async def test_gpt5_models_omit_temperature():
    client, comp = fake_client(plan_json())
    await LLMPlanner(client, "gpt-5-mini").plan(QueryRequest(query="trials by phase"))
    assert "temperature" not in comp.calls[0]


async def test_repair_loop_feeds_back_validation_errors():
    client, comp = fake_client(plan_json(dimension=None), plan_json(dimension="sponsor"))
    plan, attempts = await LLMPlanner(client, "gpt-4.1-mini").plan(QueryRequest(query="trials by sponsor"))
    assert attempts == 2 and plan.dimension == "sponsor"
    feedback = comp.calls[1]["messages"][-1]["content"]
    assert "requires 'dimension'" in feedback


async def test_gives_up_after_max_attempts():
    client, _ = fake_client("not json", plan_json(dimension=None))
    with pytest.raises(LLMPlanError):
        await LLMPlanner(client, "gpt-4.1-mini").plan(QueryRequest(query="trials by sponsor"))


async def test_planner_falls_back_to_rules_on_llm_failure():
    client, _ = fake_client(RuntimeError("connection refused"))
    planner = Planner(Settings(planner="auto"), llm_client=client)
    out = await planner.plan(QueryRequest(query="How are melanoma trials distributed across phases?"))
    assert out.info.mode == "rules" and "connection refused" in out.info.fallback_reason
    assert out.plan.dimension == "phase" and out.warnings


async def test_forced_llm_mode_raises_instead_of_falling_back():
    client, _ = fake_client(RuntimeError("boom"))
    planner = Planner(Settings(planner="auto"), llm_client=client)
    with pytest.raises(LLMPlanError):
        await planner.plan(QueryRequest(query="trials by phase", planner="llm"))


async def test_llm_hallucinated_filter_is_grounded_away_and_structured_field_wins():
    reply = plan_json(
        analysis="time_trend", dimension=None, time_field="start_date", time_granularity="year",
        filters={"intervention": "Keytruda", "statuses": ["RECRUITING"]},
    )
    client, _ = fake_client(reply)
    planner = Planner(Settings(planner="auto"), llm_client=client)
    out = await planner.plan(QueryRequest(query="How has the number of trials for this drug changed over time?", drug_name="Pembrolizumab"))
    assert out.info.mode == "llm"
    assert out.plan.filters.intervention == "Pembrolizumab"  # structured field applied
    assert out.plan.filters.statuses == []  # 'recruiting' never mentioned
    assert any("statuses" in w for w in out.warnings)
