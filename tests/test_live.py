"""Live tests against the real ClinicalTrials.gov API (and OpenAI if a key is configured).

Not run by default; use ``pytest -m live``. They check properties that only real
data can exercise: the `fields` projection and Essie filters are accepted (no
fallbacks), enum filters hold on every returned record, and citations survive an
independent re-fetch of the cited records.
"""

import pytest

from app.audit import audit, iter_data_items
from app.config import Settings
from app.ctgov import extract as ex
from app.ctgov.client import CTGovClient
from app.pipeline import Engine
from app.planner import Planner
from app.schemas.request import QueryRequest

pytestmark = pytest.mark.live


@pytest.fixture
async def live_engine():
    settings = Settings.from_env()
    client = CTGovClient(settings.ctgov_base_url)
    yield Engine(settings, client, Planner(settings))
    await client.aclose()


async def test_projection_and_advanced_filters_accepted(live_engine):
    req = QueryRequest(query="melanoma trials by sponsor class", condition="melanoma", trial_phase=["3"], start_year=2015, max_records=200, planner="rules")
    resp = await live_engine.run(req)
    log = resp.meta.api_requests[0]
    assert log.fallbacks == [], log.fallbacks
    assert log.total_count and log.total_count > 0


async def test_enum_filters_hold_on_every_record(live_engine):
    req = QueryRequest(query="recruiting phase 2 breast cancer trials by country", condition="breast cancer",
                       trial_phase=["2"], overall_status=["RECRUITING"], max_records=300, planner="rules", citation_mode="full")
    resp = (await live_engine.run(req)).model_dump(mode="json")
    assert not any("failed local filter verification" in w for w in resp["meta"]["warnings"])


@pytest.mark.parametrize(
    "query",
    [
        "Compare phases for trials involving nivolumab vs pembrolizumab",
        "Which drugs frequently co-occur in combination studies for pancreatic cancer?",
        "How many trials started each year for cystic fibrosis since 2010?",
    ],
)
async def test_citations_survive_independent_refetch(live_engine, query):
    resp = (await live_engine.run(QueryRequest(query=query, max_records=300))).model_dump(mode="json")
    cited = sorted({c["nct_id"] for _, it in iter_data_items(resp["visualization"]) for c in it.get("citations", [])})[:10]
    assert cited
    records = {n: await live_engine.client.get_study(n) for n in cited}
    report = audit(resp, records, nct_ids=set(cited))
    assert report.ok, report.failures
    assert all(ex.nct_id(r) == n for n, r in records.items())
