"""Shared fixtures: a fake ClinicalTrials.gov API (httpx.MockTransport) serving synthetic records."""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from fixtures.factory import corpus  # noqa: E402

from app.config import Settings  # noqa: E402
from app.ctgov import extract as ex  # noqa: E402
from app.ctgov.client import CTGovClient  # noqa: E402
from app.pipeline import Engine  # noqa: E402
from app.planner import Planner  # noqa: E402

BASE = "https://ctgov.test/api/v2"


def _texts(study: dict[str, Any], area: str) -> list[str]:
    ps = study["protocolSection"]
    if area == "intr":
        out = []
        for i in ps["armsInterventionsModule"]["interventions"]:
            out.append(i["name"])
            out += i.get("otherNames", [])
        return out
    if area == "cond":
        return ps["conditionsModule"]["conditions"]
    if area == "spons":
        sc = ps["sponsorCollaboratorsModule"]
        return [sc["leadSponsor"]["name"]] + [c["name"] for c in sc.get("collaborators", [])]
    if area == "locn":
        return [loc["country"] for loc in ps["contactsLocationsModule"].get("locations", [])]
    return [json.dumps(study)]


class FakeCTGov:
    """Minimal emulation of /studies and /version, with switches to inject failures."""

    def __init__(self, studies: list[dict[str, Any]] | None = None):
        self.studies = studies if studies is not None else corpus()
        self.requests: list[httpx.Request] = []
        self.reject_fields = False
        self.reject_advanced = False
        self.fail_times = 0  # respond 503 this many times first

    def matches(self, s: dict[str, Any], params: dict[str, str]) -> bool:
        for key, area in (("query.intr", "intr"), ("query.cond", "cond"), ("query.spons", "spons"),
                          ("query.locn", "locn"), ("query.term", "term")):
            if key in params:
                needles = [n.strip("() ").lower() for n in re.split(r"\s+AND\s+", params[key])]
                hay = " ".join(_texts(s, area)).lower()
                if not all(n in hay for n in needles):
                    return False
        if "filter.overallStatus" in params:
            if ex.get_path(s, ex.P_STATUS) not in params["filter.overallStatus"].split(","):
                return False
        adv = params.get("filter.advanced", "")
        if m := re.search(r"AREA\[Phase\]\(([^)]*)\)", adv):
            wanted = set(m.group(1).split(" OR "))
            if not wanted & set(ex.get_path(s, ex.P_PHASES) or []):
                return False
        if m := re.search(r"AREA\[StartDate\]RANGE\[([^,]+),([^\]]+)\]", adv):
            y = ex.year_of(ex.get_path(s, ex.TIME_PATHS["start_date"]))
            lo, hi = m.group(1), m.group(2)
            if y is None or (lo != "MIN" and y < int(lo[:4])) or (hi != "MAX" and y > int(hi[:4])):
                return False
        return True

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if self.fail_times > 0:
            self.fail_times -= 1
            return httpx.Response(503, text="busy")
        path = request.url.path
        params = dict(request.url.params)
        if path.endswith("/version"):
            return httpx.Response(200, json={"apiVersion": "2.0.test", "dataTimestamp": "2026-10-01T00:00:00"})
        if path.endswith("/studies"):
            if self.reject_fields and "fields" in params:
                return httpx.Response(400, text="unknown field")
            if self.reject_advanced and "filter.advanced" in params:
                return httpx.Response(400, text="bad expression")
            hits = [s for s in self.studies if self.matches(s, params)]
            size = int(params.get("pageSize", 10))
            offset = int(params.get("pageToken", 0))
            body: dict[str, Any] = {"studies": hits[offset : offset + size]}
            if offset + size < len(hits):
                body["nextPageToken"] = str(offset + size)
            if params.get("countTotal") == "true":
                body["totalCount"] = len(hits)
            return httpx.Response(200, json=body)
        if m := re.search(r"/studies/(NCT\d+)$", path):
            s = next((s for s in self.studies if ex.nct_id(s) == m.group(1)), None)
            return httpx.Response(200, json=s) if s else httpx.Response(404)
        return httpx.Response(404)


@pytest.fixture
def fake_api() -> FakeCTGov:
    return FakeCTGov()


@pytest.fixture
def settings() -> Settings:
    return Settings(openai_api_key=None, planner="rules", ctgov_base_url=BASE, default_max_records=1000)


@pytest.fixture
def client(fake_api: FakeCTGov) -> CTGovClient:
    return CTGovClient(BASE, transport=httpx.MockTransport(fake_api.handler), backoff_base_s=0.0)


@pytest.fixture
def engine(settings: Settings, client: CTGovClient) -> Engine:
    return Engine(settings, client, Planner(settings))


@pytest.fixture
def studies_by_id() -> dict[str, dict[str, Any]]:
    return {ex.nct_id(s): s for s in corpus()}
