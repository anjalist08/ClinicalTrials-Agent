"""End-to-end orchestration: plan -> fetch -> verify -> aggregate -> visualize -> describe.

    QueryRequest
        │  Planner (LLM w/ structured outputs, or rules) → grounding → overrides → validation
        ▼
    QueryPlan ──► one API query per dataset (base filters, or one per comparison group)
        │         paging, retries, 400-fallbacks, cache          (app.ctgov.client)
        ▼
    raw study records ──► local verification of enum/range filters (app.ctgov.query_builder)
        │
        ▼
    deterministic aggregation with evidence (app.analytics) ──► viz spec + citations (app.viz)
        ▼
    QueryResponse (visualization + meta: plan, filters, API URLs, completeness, warnings, source)
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .analytics.citations import CitationFactory
from .config import Settings
from .ctgov import extract as ex
from .ctgov.client import CTGovClient, SearchResult
from .ctgov.query_builder import build_params, merge_filters, record_matches
from .planner import Planner, PlanOutcome
from .schemas.plan import Filters
from .schemas.request import QueryRequest
from .schemas.response import ApiRequestLog, DataCompleteness, Meta, QueryResponse, SourceInfo
from .viz.builder import Dataset, build


@dataclass
class FetchOutcome:
    dataset: Dataset
    log: ApiRequestLog
    warnings: list[str] = field(default_factory=list)


class Engine:
    def __init__(self, settings: Settings, client: CTGovClient, planner: Planner):
        self.settings = settings
        self.client = client
        self.planner = planner
        self._version: dict[str, Any] | None = None

    async def plan(self, req: QueryRequest) -> PlanOutcome:
        return await self.planner.plan(req)

    async def _source_info(self) -> SourceInfo:
        if self._version is None:
            try:
                self._version = await self.client.version()
            except Exception:  # version is informative only; never fail a query over it
                self._version = {}
        return SourceInfo(
            api=self.settings.ctgov_base_url,
            api_version=self._version.get("apiVersion"),
            data_timestamp=self._version.get("dataTimestamp"),
            retrieved_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
        )

    async def _fetch(self, label: str | None, filters: Filters, max_records: int) -> FetchOutcome:
        warnings: list[str] = []
        res: SearchResult = await self.client.search(build_params(filters), max_records)

        # Zero-result recovery: an area-specific search (intervention / condition) can miss trials that
        # mention the entity elsewhere; retry once as a full-text search and say so.
        if res.total_count == 0 and (filters.intervention or filters.condition) and not filters.term:
            area, value = ("intervention", filters.intervention) if filters.intervention else ("condition", filters.condition)
            relaxed = filters.model_copy(update={area: None, "term": value})
            res2 = await self.client.search(build_params(relaxed), max_records)
            if res2.total_count:
                warnings.append(
                    f"No trials matched {area}={value!r}{f' in group {label!r}' if label else ''}; "
                    "retried as a full-text search across all fields."
                )
                res, filters = res2, relaxed

        verified = [s for s in res.studies if record_matches(s, filters)]
        dropped = len(res.studies) - len(verified)
        total = res.total_count
        if not res.used_advanced_filter:
            warnings.append(
                "The API rejected the advanced (phase/date/type) filter expression; those filters were applied "
                "locally to the retrieved records, so the reported total is an upper bound."
            )
            total = None if dropped else total
        elif dropped:
            warnings.append(f"{dropped} record(s) returned by the API failed local filter verification and were excluded.")

        log = ApiRequestLog(
            group=label,
            url=res.url,
            total_count=res.total_count,
            retrieved=len(res.studies),
            pages=res.pages,
            from_cache=res.from_cache,
            fallbacks=res.fallbacks,
        )
        return FetchOutcome(Dataset(label, filters, verified, total), log, warnings)

    async def run(self, req: QueryRequest) -> QueryResponse:
        t0 = time.perf_counter()
        outcome = await self.planner.plan(req)
        plan = outcome.plan
        t_plan = time.perf_counter()

        max_records = req.max_records or self.settings.default_max_records
        if plan.analysis == "total" and req.citation_mode != "full":
            max_records = min(max_records, 100)  # the count comes from countTotal; records only feed citations
        specs = [(g.label, merge_filters(plan.filters, g.filters)) for g in plan.groups] or [(None, plan.filters)]
        fetched, source = await asyncio.gather(
            asyncio.gather(*(self._fetch(label, f, max_records) for label, f in specs)),
            self._source_info(),
        )
        t_fetch = time.perf_counter()

        datasets = [f.dataset for f in fetched]
        warnings = list(outcome.warnings)
        for f in fetched:
            warnings += f.warnings

        studies_by_id = {ex.nct_id(s): s for d in datasets for s in d.studies}
        text_filters = dict(plan.filters.text_items())
        for g in plan.groups:
            for k, v in g.filters.text_items().items():
                text_filters.setdefault(k, v)
        cites = CitationFactory(studies_by_id, text_filters, req.citation_mode, req.max_citations_per_datum)
        result = build(plan, datasets, cites)
        t_build = time.perf_counter()
        warnings += result.warnings

        completeness = self._completeness(datasets, plan.analysis)
        if not completeness.complete and completeness.note:
            warnings.append(completeness.note)
        if not studies_by_id:
            warnings.append("No trials matched the query; the visualization is empty.")

        meta = Meta(
            query=req.query,
            interpretation=plan.interpretation,
            visualization_rationale=result.rationale,
            plan=plan,
            planner=outcome.info,
            filters_applied=plan.filters,
            groups=[{"label": d.label, "filters": d.filters.model_dump(exclude_defaults=True)} for d in datasets if d.label],
            counting_rule=result.counting_rule,
            rendering=result.rendering,
            data_completeness=completeness,
            citations={
                "mode": req.citation_mode,
                "max_per_datum": req.max_citations_per_datum if req.citation_mode == "summary" else None,
                "excerpt_semantics": "evidence[].excerpt is the exact value at evidence[].path in the API v2 record "
                "(derived=true marks values the datum is computed from, e.g. a location count).",
            },
            api_requests=[f.log for f in fetched],
            assumptions=plan.assumptions + outcome.notes + result.assumptions,
            warnings=list(dict.fromkeys(warnings)),
            source=source,
            timing_ms={
                "plan": int((t_plan - t0) * 1000),
                "fetch": int((t_fetch - t_plan) * 1000),
                "aggregate": int((t_build - t_fetch) * 1000),
                "total": int((t_build - t0) * 1000),
            },
        )
        return QueryResponse(visualization=result.visualization, meta=meta)

    @staticmethod
    def _completeness(datasets: list[Dataset], analysis: str) -> DataCompleteness:
        analyzed = sum(len(d.studies) for d in datasets)
        totals = [d.total_count for d in datasets]
        known = all(t is not None for t in totals)
        total = sum(t for t in totals if t is not None) if known else None
        complete = known and all(len(d.studies) >= (d.total_count or 0) for d in datasets)
        note = None
        if analysis == "total":
            complete = known
            note = None if known else "Exact total unavailable (server-side filter fallback)."
        elif not complete:
            note = (
                f"Analysed {analyzed:,} of {total:,} matching trials (API relevance order); counts are for this "
                "subset. Raise max_records (up to 10,000) for complete counts."
                if total is not None
                else "The total number of matching trials is unknown; counts cover the retrieved records."
            )
        return DataCompleteness(total_matching=total, records_analyzed=analyzed, complete=complete, note=note)
