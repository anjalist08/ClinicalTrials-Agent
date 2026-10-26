"""Async client for the ClinicalTrials.gov Data API v2 (https://clinicaltrials.gov/data-api/api).

Responsibilities:

* paging (``pageToken``) up to a record cap, with ``countTotal`` so callers know
  whether the analysed set is complete;
* retries with exponential backoff on 429 / 5xx / network errors (honours
  ``Retry-After``);
* a graceful-degradation ladder for HTTP 400: if the server rejects the
  ``fields`` projection or the ``filter.advanced`` expression, retry without it.
  Dropped server-side filters are re-applied locally by the caller, and every
  fallback is reported in the response metadata;
* an in-memory TTL cache, optionally mirrored to disk (makes example runs and
  demos reproducible and polite to the public API).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from .query_builder import FIELDS

log = logging.getLogger(__name__)

MAX_PAGE_SIZE = 1000


class CTGovError(RuntimeError):
    """The API could not be reached or returned an unrecoverable error."""


class CTGovBadRequest(CTGovError):
    pass


@dataclass
class SearchResult:
    studies: list[dict[str, Any]]
    total_count: int | None
    pages: int
    url: str
    from_cache: bool = False
    fallbacks: list[str] = field(default_factory=list)
    used_advanced_filter: bool = True


class ResponseCache:
    def __init__(self, ttl_s: int, directory: Path | None = None, max_items: int = 256):
        self.ttl_s = ttl_s
        self.directory = directory
        self.max_items = max_items
        self._mem: dict[str, tuple[float, Any]] = {}
        if directory:
            directory.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def key(path: str, params: dict[str, Any]) -> str:
        raw = json.dumps([path, sorted(params.items())], default=str)
        return hashlib.sha256(raw.encode()).hexdigest()[:32]

    def get(self, key: str) -> Any | None:
        hit = self._mem.get(key)
        if hit and time.time() - hit[0] < self.ttl_s:
            return hit[1]
        if self.directory:
            p = self.directory / f"{key}.json"
            if p.is_file() and time.time() - p.stat().st_mtime < self.ttl_s:
                value = json.loads(p.read_text())
                self._mem[key] = (time.time(), value)
                return value
        return None

    def set(self, key: str, value: Any) -> None:
        if len(self._mem) >= self.max_items:
            oldest = min(self._mem, key=lambda k: self._mem[k][0])
            self._mem.pop(oldest, None)
        self._mem[key] = (time.time(), value)
        if self.directory:
            (self.directory / f"{key}.json").write_text(json.dumps(value))


class CTGovClient:
    def __init__(
        self,
        base_url: str = "https://clinicaltrials.gov/api/v2",
        *,
        timeout_s: float = 30.0,
        max_retries: int = 3,
        cache: ResponseCache | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        backoff_base_s: float = 0.5,
        max_concurrency: int = 4,
    ):
        self.base_url = base_url.rstrip("/")
        self.max_retries = max_retries
        self.cache = cache
        self.backoff_base_s = backoff_base_s
        self._http = httpx.AsyncClient(
            timeout=timeout_s,
            transport=transport,
            headers={"Accept": "application/json", "User-Agent": "ctgov-viz-agent/1.0"},
        )
        self._sem = asyncio.Semaphore(max_concurrency)
        # Learned once per process: whether the server accepts our `fields` projection.
        self._fields_supported: bool | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    # ------------------------------------------------------------------ low level
    async def _get_json(self, path: str, params: dict[str, Any]) -> tuple[Any, bool]:
        """GET with cache + retries. Returns ``(json, from_cache)``."""
        key = ResponseCache.key(path, params)
        if self.cache and (hit := self.cache.get(key)) is not None:
            return hit, True

        url = f"{self.base_url}{path}"
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                async with self._sem:
                    resp = await self._http.get(url, params=params)
            except httpx.HTTPError as exc:  # network error / timeout
                last_exc = exc
                log.warning("ctgov network error (attempt %d): %s", attempt + 1, exc)
            else:
                if resp.status_code == 200:
                    data = resp.json()
                    if self.cache:
                        self.cache.set(key, data)
                    return data, False
                if resp.status_code == 400:
                    raise CTGovBadRequest(f"400 from ClinicalTrials.gov: {resp.text[:300]}")
                if resp.status_code not in (429, 500, 502, 503, 504):
                    raise CTGovError(f"HTTP {resp.status_code} from ClinicalTrials.gov: {resp.text[:300]}")
                last_exc = CTGovError(f"HTTP {resp.status_code}")
                retry_after = resp.headers.get("Retry-After")
                if retry_after and retry_after.isdigit() and attempt < self.max_retries:
                    await asyncio.sleep(min(float(retry_after), 10.0))
                    continue
            if attempt < self.max_retries:
                await asyncio.sleep(self.backoff_base_s * 2**attempt)
        raise CTGovError(f"ClinicalTrials.gov unavailable after {self.max_retries + 1} attempts: {last_exc}")

    # ------------------------------------------------------------------ public
    async def version(self) -> dict[str, Any]:
        data, _ = await self._get_json("/version", {})
        return data

    async def get_study(self, nct_id: str) -> dict[str, Any]:
        """Full record for one trial (no field projection) - used for independent citation audits."""
        data, _ = await self._get_json(f"/studies/{nct_id}", {"format": "json"})
        return data

    async def search(self, params: dict[str, Any], max_records: int) -> SearchResult:
        """Fetch up to ``max_records`` studies matching ``params`` (API query/filter params)."""
        ladder = self._param_ladder(params)
        last_error: CTGovBadRequest | None = None
        for variant, fallbacks in ladder:
            try:
                result = await self._paged_search(variant, max_records)
            except CTGovBadRequest as exc:
                last_error = exc
                log.warning("ctgov rejected params %s: %s", variant, exc)
                continue
            if "fields" in variant:
                self._fields_supported = True
            elif self._fields_supported is None and "fields_rejected" in fallbacks:
                self._fields_supported = False
            result.fallbacks = fallbacks
            result.used_advanced_filter = "filter.advanced" in variant or "filter.advanced" not in params
            return result
        raise last_error or CTGovError("search failed")

    def _param_ladder(self, params: dict[str, Any]) -> list[tuple[dict[str, Any], list[str]]]:
        fields = {"fields": ",".join(FIELDS)} if self._fields_supported is not False else {}
        no_adv = {k: v for k, v in params.items() if k != "filter.advanced"}
        ladder: list[tuple[dict[str, Any], list[str]]] = []
        base_fb = [] if self._fields_supported is not False else ["fields_rejected"]
        ladder.append(({**params, **fields}, list(base_fb)))
        if fields:
            ladder.append((dict(params), ["fields_rejected"]))
        if "filter.advanced" in params:
            ladder.append(({**no_adv, **fields}, base_fb + ["advanced_filter_rejected"]))
            if fields:
                ladder.append((no_adv, ["fields_rejected", "advanced_filter_rejected"]))
        return ladder

    async def _paged_search(self, params: dict[str, Any], max_records: int) -> SearchResult:
        page_size = max(1, min(MAX_PAGE_SIZE, max_records))
        query = {**params, "format": "json", "countTotal": "true", "pageSize": page_size}
        url = f"{self.base_url}/studies?{urlencode(query)}"
        studies: list[dict[str, Any]] = []
        total: int | None = None
        pages = 0
        all_cached = True
        token: str | None = None
        while True:
            page_params = dict(query)
            if token:
                page_params["pageToken"] = token
            data, cached = await self._get_json("/studies", page_params)
            all_cached = all_cached and cached
            pages += 1
            if total is None:
                total = data.get("totalCount")
            studies.extend(data.get("studies") or [])
            token = data.get("nextPageToken")
            if not token or len(studies) >= max_records:
                break
        return SearchResult(
            studies=studies[:max_records], total_count=total, pages=pages, url=url, from_cache=all_cached
        )
