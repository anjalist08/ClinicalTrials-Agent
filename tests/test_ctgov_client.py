import httpx
import pytest
from conftest import BASE
from fixtures.factory import corpus

from app.ctgov.client import CTGovClient, CTGovError, ResponseCache
from app.ctgov.query_builder import build_params, essie_clauses, merge_filters, record_matches
from app.schemas.plan import Filters


def make_client(api, **kw):
    return CTGovClient(BASE, transport=httpx.MockTransport(api.handler), backoff_base_s=0.0, **kw)


def test_build_params_maps_areas_and_essie():
    f = Filters(condition="melanoma", intervention="nivolumab", country="France", statuses=["RECRUITING", "COMPLETED"],
                phases=["PHASE2", "PHASE3"], start_year_from=2015, sponsor_classes=["INDUSTRY"])
    p = build_params(f)
    assert p["query.cond"] == "melanoma" and p["query.intr"] == "nivolumab" and p["query.locn"] == "France"
    assert p["filter.overallStatus"] == "RECRUITING,COMPLETED"
    assert p["filter.advanced"] == (
        "AREA[Phase](PHASE2 OR PHASE3) AND AREA[LeadSponsorClass](INDUSTRY) AND AREA[StartDate]RANGE[2015-01-01,MAX]"
    )
    assert "filter.advanced" not in build_params(f, use_advanced=False)


def test_essie_year_range_bounds():
    assert essie_clauses(Filters(start_year_to=2010)) == ["AREA[StartDate]RANGE[MIN,2010-12-31]"]


def test_merge_filters_group_overrides_base():
    m = merge_filters(Filters(condition="melanoma", phases=["PHASE3"]), Filters(intervention="nivolumab"))
    assert m.condition == "melanoma" and m.intervention == "nivolumab" and m.phases == ["PHASE3"]


def test_record_matches_local_verification():
    s = {x["protocolSection"]["identificationModule"]["nctId"]: x for x in corpus()}
    assert record_matches(s["NCT90000001"], Filters(phases=["PHASE3"], statuses=["COMPLETED"]))
    assert not record_matches(s["NCT90000001"], Filters(phases=["PHASE2"]))
    assert not record_matches(s["NCT90000008"], Filters(start_year_from=2010))  # no start date
    assert record_matches(s["NCT90000002"], Filters(phases=["PHASE2"]))  # Phase 1/2 counts as phase 2
    assert not record_matches(s["NCT90000005"], Filters(intervention_types=["DRUG"]))


async def test_paging_collects_all_pages(fake_api):
    c = make_client(fake_api)
    res = await c.search({}, max_records=3)
    assert len(res.studies) == 3 and res.total_count == 8 and res.pages == 1
    res = await c.search({"query.term": "melanoma"}, max_records=1000)
    assert res.total_count == len(res.studies) == 3


async def test_multi_page_pagination(fake_api):
    from app.ctgov import client as client_mod

    old = client_mod.MAX_PAGE_SIZE
    client_mod.MAX_PAGE_SIZE = 3
    try:
        res = await make_client(fake_api).search({}, max_records=7)
    finally:
        client_mod.MAX_PAGE_SIZE = old
    assert len(res.studies) == 7 and res.pages == 3
    tokens = [r.url.params.get("pageToken") for r in fake_api.requests if r.url.path.endswith("/studies")]
    assert tokens == [None, "3", "6"]


async def test_retries_transient_errors(fake_api):
    fake_api.fail_times = 2
    res = await make_client(fake_api).search({}, max_records=10)
    assert res.total_count == 8


async def test_gives_up_after_retries(fake_api):
    fake_api.fail_times = 10
    with pytest.raises(CTGovError):
        await make_client(fake_api, max_retries=2).search({}, max_records=10)


async def test_fields_rejection_falls_back_and_is_remembered(fake_api):
    fake_api.reject_fields = True
    c = make_client(fake_api)
    res = await c.search({}, max_records=10)
    assert res.fallbacks == ["fields_rejected"] and len(res.studies) == 8
    n = len(fake_api.requests)
    await c.search({"query.term": "melanoma"}, max_records=10)
    assert len(fake_api.requests) == n + 1  # no second failed attempt


async def test_advanced_filter_rejection_reported(fake_api):
    fake_api.reject_advanced = True
    res = await make_client(fake_api).search({"filter.advanced": "AREA[Phase](PHASE3)"}, max_records=10)
    assert "advanced_filter_rejected" in res.fallbacks and res.used_advanced_filter is False


async def test_cache_avoids_repeat_requests(fake_api, tmp_path):
    c = make_client(fake_api, cache=ResponseCache(ttl_s=60, directory=tmp_path))
    await c.search({"query.term": "melanoma"}, 10)
    n = len(fake_api.requests)
    res = await c.search({"query.term": "melanoma"}, 10)
    assert len(fake_api.requests) == n and res.from_cache
    # disk mirror survives a new client
    c2 = make_client(fake_api, cache=ResponseCache(ttl_s=60, directory=tmp_path))
    res2 = await c2.search({"query.term": "melanoma"}, 10)
    assert res2.from_cache and len(fake_api.requests) == n
