"""End-to-end through the HTTP layer with the fake ClinicalTrials.gov API.

Every visualization type is produced from a natural-language query, validated
against the response schema, and every citation is audited against the source
records.
"""

import pytest
from fastapi.testclient import TestClient

from app.audit import audit
from app.main import create_app
from app.schemas.response import QueryResponse


@pytest.fixture
def api(settings, engine):
    with TestClient(create_app(settings, engine)) as c:
        yield c


CASES = [
    ("How are pembrolizumab trials distributed across phases?", "bar_chart"),
    ("How many trials started each year for melanoma since 2014?", "time_series"),
    ("Compare phases for trials involving nivolumab vs pembrolizumab", "grouped_bar_chart"),
    ("Show a network of sponsors and drugs", "network_graph"),
    ("Which drugs co-occur in combination studies?", "network_graph"),
    ("Distribution of enrollment sizes", "histogram"),
    ("Scatter of enrollment vs duration", "scatter_plot"),
    ("Show a world map of trial countries", "choropleth_map"),
    ("How many recruiting trials are there?", "kpi"),
    ("Show the largest trials", "table"),
    ("Breakdown of trials by phase and status", "stacked_bar_chart"),
    ("Which countries have the most trials?", "bar_chart"),
]


@pytest.mark.parametrize("query,viz_type", CASES)
def test_query_produces_valid_audited_visualization(api, studies_by_id, query, viz_type):
    r = api.post("/v1/query", json={"query": query})
    assert r.status_code == 200, r.text
    body = r.json()
    QueryResponse.model_validate(body)  # round-trips through the documented schema
    vis, meta = body["visualization"], body["meta"]
    assert vis["type"] == viz_type
    # every encoded field exists in the data
    rows = vis["data"]["nodes"] + vis["data"]["edges"] if viz_type == "network_graph" else vis["data"]
    assert rows, "expected data"
    for channel in vis["encoding"].values():
        if viz_type == "network_graph":
            continue
        assert any(channel["field"] in row for row in rows), channel
    report = audit(body, studies_by_id)
    assert report.ok, report.failures
    assert meta["source"]["api_version"] == "2.0.test"
    assert meta["api_requests"][0]["url"].startswith("https://ctgov.test/api/v2/studies?")


def test_time_series_has_explicit_zero_years(api):
    body = api.post("/v1/query", json={"query": "How many trials started each year for melanoma since 2014?"}).json()
    rows = {r["period"]: r["trial_count"] for r in body["visualization"]["data"]}
    assert rows == {"2014": 0, "2015": 1, "2016": 0, "2017": 0, "2018": 1, "2019": 1}


def test_structured_drug_name_and_full_citations(api, studies_by_id):
    body = api.post(
        "/v1/query",
        json={"query": "How has the number of trials for this drug changed over time?", "drug_name": "Pembrolizumab",
              "citation_mode": "full"},
    ).json()
    assert body["meta"]["filters_applied"]["intervention"] == "Pembrolizumab"
    assert body["visualization"]["type"] == "time_series"
    total = sum(r["trial_count"] for r in body["visualization"]["data"])
    assert total == 3  # NCT90000001, -02, -07
    assert audit(body, studies_by_id, search_terms=["pembrolizumab"]).ok
    cited = {c["nct_id"] for r in body["visualization"]["data"] for c in r.get("citations", [])}
    assert cited == {"NCT90000001", "NCT90000002", "NCT90000007"}


def test_percent_metric_for_comparison(api):
    body = api.post(
        "/v1/query",
        json={"query": "Compare the share of trials by phase", "compare": {"field": "drug", "values": ["nivolumab", "pembrolizumab"]}},
    ).json()
    rows = body["visualization"]["data"]
    assert body["visualization"]["encoding"]["y"]["field"] == "percent"
    for g in ("nivolumab", "pembrolizumab"):
        assert sum(r["percent"] for r in rows if r["group"] == g) == pytest.approx(100, abs=0.2)


def test_citations_off(api):
    body = api.post("/v1/query", json={"query": "trials by phase", "citation_mode": "off"}).json()
    assert all("citations" not in r and r["citation_count"] >= 0 for r in body["visualization"]["data"])


def test_incomplete_data_is_flagged(api):
    body = api.post("/v1/query", json={"query": "trials by phase", "max_records": 3}).json()
    dc = body["meta"]["data_completeness"]
    assert dc == {"total_matching": 8, "records_analyzed": 3, "complete": False, "note": dc["note"]}
    assert any("Analysed 3 of 8" in w for w in body["meta"]["warnings"])


def test_kpi_uses_exact_total_count(api):
    body = api.post("/v1/query", json={"query": "How many trials are there?", "max_records": 2}).json()
    assert body["visualization"]["data"][0]["trial_count"] == 8
    assert body["meta"]["data_completeness"]["complete"] is True


def test_zero_result_recovery_retries_as_full_text(api, fake_api):
    # "Keytruda" is only an otherName -> matches query.intr in the fake; use a title-only word instead
    body = api.post("/v1/query", json={"query": "trials by phase", "drug_name": "Exercise Program"}).json()
    assert any("retried as a full-text search" in w for w in body["meta"]["warnings"])
    assert sum(r["trial_count"] for r in body["visualization"]["data"]) == 1


def test_advanced_filter_rejection_is_applied_locally(api, fake_api):
    fake_api.reject_advanced = True
    body = api.post("/v1/query", json={"query": "phase 3 trials by sponsor"}).json()
    assert body["meta"]["api_requests"][0]["fallbacks"] == ["advanced_filter_rejected"]
    # NCT90000001, -03 (Phase 3) and -08 (Phase 2/3): same semantics as the API's AREA[Phase] filter
    assert sum(r["trial_count"] for r in body["visualization"]["data"]) == 3
    assert any("applied locally" in w for w in body["meta"]["warnings"])


def test_empty_result_is_valid_and_explained(api):
    body = api.post("/v1/query", json={"query": "trials by phase", "keyword": "zzzz-nothing"}).json()
    assert body["visualization"]["data"] == []
    assert any("No trials matched" in w for w in body["meta"]["warnings"])


def test_plan_endpoint_dry_run(api, fake_api):
    r = api.post("/v1/plan", json={"query": "Compare phases for trials involving nivolumab vs pembrolizumab"})
    body = r.json()
    assert body["plan"]["analysis"] == "distribution"
    assert [p["params"] for p in body["api_params"]] == [{"query.intr": "nivolumab"}, {"query.intr": "pembrolizumab"}]
    assert not any(req.url.path.endswith("/studies") for req in fake_api.requests)


def test_invalid_request_422(api):
    assert api.post("/v1/query", json={"query": "x"}).status_code == 422
    assert api.post("/v1/query", json={"query": "valid", "trial_phase": ["phase 9"]}).status_code == 422


def test_schema_and_health(api):
    s = api.get("/v1/schema").json()
    assert {"request", "response", "plan"} <= set(s)
    assert "query" in s["request"]["required"]
    h = api.get("/health").json()
    assert h == {"status": "ok", "planner": "rules", "model": None, "ctgov_api": "https://ctgov.test/api/v2"}


def test_ui_is_served(api):
    r = api.get("/")
    assert r.status_code == 200 and "<html" in r.text.lower()
