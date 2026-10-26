import pytest
from pydantic import ValidationError

from app.schemas.request import QueryRequest, normalize_phase


@pytest.mark.parametrize(
    "raw,expected",
    [("Phase 3", "PHASE3"), ("phase III", "PHASE3"), ("3", "PHASE3"), ("PHASE2", "PHASE2"), (4, "PHASE4"),
     ("early phase 1", "EARLY_PHASE1"), ("Early_Phase1", "EARLY_PHASE1"), ("n/a", "NA"), ("phase iv", "PHASE4")],
)
def test_phase_normalisation(raw, expected):
    assert normalize_phase(raw) == expected


def test_phase_rejects_garbage():
    with pytest.raises(ValueError):
        normalize_phase("phase 7")


def test_minimal_request():
    r = QueryRequest(query="How many trials for melanoma?")
    assert r.citation_mode == "summary" and r.planner == "auto"


def test_structured_fields_are_normalised_and_deduplicated():
    r = QueryRequest(
        query="phases please",
        trial_phase=["Phase 2", "phase II", "3"],
        overall_status=["recruiting", "Not yet recruiting"],
        study_type="interventional",
        drug_name="  Pembrolizumab ",
    )
    assert r.trial_phase == ["PHASE2", "PHASE3"]
    assert r.overall_status == ["RECRUITING", "NOT_YET_RECRUITING"]
    assert r.study_type == "INTERVENTIONAL"
    assert r.drug_name == "Pembrolizumab"


@pytest.mark.parametrize(
    "payload",
    [
        {"query": ""},
        {"query": "ok query", "start_year": 2020, "end_year": 2010},
        {"query": "ok query", "overall_status": ["SLEEPING"]},
        {"query": "ok query", "unknown_field": 1},
        {"query": "ok query", "compare": {"field": "drug", "values": ["a"]}},
        {"query": "ok query", "compare": {"field": "drug", "values": ["a", "A"]}},
        {"query": "ok query", "top_n": 0},
        {"query": "ok query", "planner": "magic"},
    ],
)
def test_invalid_requests_rejected(payload):
    with pytest.raises(ValidationError):
        QueryRequest.model_validate(payload)
