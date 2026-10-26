"""HTTP API (FastAPI).

Endpoints
---------
POST /v1/query    question (+ optional structured fields) -> visualization spec + metadata
POST /v1/plan     dry run: the validated plan and the API parameters it would use (no data fetched)
GET  /v1/schema   JSON Schemas for request, response and plan
GET  /health      liveness + planner configuration
GET  /            demo UI (web/index.html)
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .config import PROJECT_ROOT, Settings
from .ctgov.client import CTGovClient, CTGovError, ResponseCache
from .ctgov.query_builder import build_params, merge_filters
from .pipeline import Engine
from .planner import LLMPlanError, Planner, PlanValidationError
from .schemas.plan import QueryPlan
from .schemas.request import QueryRequest
from .schemas.response import ErrorResponse, QueryResponse

log = logging.getLogger("ctgov_agent")
WEB_DIR = PROJECT_ROOT / "web"


def create_app(settings: Settings | None = None, engine: Engine | None = None) -> FastAPI:
    settings = settings or Settings.from_env()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if engine is not None:
            app.state.engine = engine
            yield
            return
        cache = ResponseCache(settings.cache_ttl_s, settings.cache_dir)
        client = CTGovClient(
            settings.ctgov_base_url, timeout_s=settings.ctgov_timeout_s, max_retries=settings.ctgov_max_retries, cache=cache
        )
        app.state.engine = Engine(settings, client, Planner(settings))
        try:
            yield
        finally:
            await client.aclose()

    app = FastAPI(
        title="ClinicalTrials.gov Query-to-Visualization Agent",
        version="1.0.0",
        description="Turns clinical-trial questions into frontend-ready visualization specs backed by "
        "ClinicalTrials.gov API v2 data, with deep citations to source records.",
        lifespan=lifespan,
    )
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

    @app.exception_handler(PlanValidationError)
    async def _plan_error(_: Request, exc: PlanValidationError):
        return JSONResponse(status_code=422, content=ErrorResponse(error="invalid_plan", detail=exc.errors).model_dump())

    @app.exception_handler(LLMPlanError)
    async def _llm_error(_: Request, exc: LLMPlanError):
        return JSONResponse(status_code=503, content=ErrorResponse(error="planner_unavailable", detail=str(exc)).model_dump())

    @app.exception_handler(CTGovError)
    async def _ctgov_error(_: Request, exc: CTGovError):
        return JSONResponse(status_code=502, content=ErrorResponse(error="upstream_error", detail=str(exc)).model_dump())

    @app.post("/v1/query", response_model=QueryResponse, response_model_exclude_none=True, responses={422: {"model": ErrorResponse}, 502: {"model": ErrorResponse}})
    async def query(req: QueryRequest, request: Request) -> QueryResponse:
        return await request.app.state.engine.run(req)

    @app.post("/v1/plan")
    async def plan(req: QueryRequest, request: Request) -> dict:
        outcome = await request.app.state.engine.plan(req)
        p = outcome.plan
        specs = [(g.label, merge_filters(p.filters, g.filters)) for g in p.groups] or [(None, p.filters)]
        return {
            "plan": p.model_dump(),
            "planner": outcome.info.model_dump(),
            "warnings": outcome.warnings,
            "notes": outcome.notes,
            "api_params": [{"group": label, "params": build_params(f)} for label, f in specs],
        }

    @app.get("/v1/schema")
    async def schema() -> dict:
        return {
            "request": QueryRequest.model_json_schema(),
            "response": QueryResponse.model_json_schema(),
            "plan": QueryPlan.model_json_schema(),
        }

    @app.get("/health")
    async def health(request: Request) -> dict:
        eng: Engine = request.app.state.engine
        return {
            "status": "ok",
            "planner": "llm" if eng.planner.llm else "rules",
            "model": eng.planner.llm.model if eng.planner.llm else None,
            "ctgov_api": settings.ctgov_base_url,
        }

    if WEB_DIR.is_dir():
        app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(WEB_DIR / "index.html")

    return app


app = create_app()
