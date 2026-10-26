from fixtures.factory import corpus, study

from app.ctgov import extract as ex


def by_id():
    return {ex.nct_id(s): s for s in corpus()}


def test_get_path_handles_lists_and_missing():
    s = by_id()["NCT90000001"]
    assert ex.get_path(s, "protocolSection.designModule.phases[0]") == "PHASE3"
    assert ex.get_path(s, "protocolSection.designModule.phases[5]") is None
    assert ex.get_path(s, "protocolSection.nope.x") is None


def test_phase_labels_and_combined_phases():
    s = by_id()
    assert [v.label for v in ex.DIMENSIONS["phase"].extract(s["NCT90000002"])] == ["Phase 1/2"]
    vals = ex.DIMENSIONS["phase"].extract(s["NCT90000002"])
    assert [e.excerpt for e in vals[0].evidence] == ["PHASE1", "PHASE2"]
    # observational study has no phases field -> explicit missing bucket with derived evidence
    missing = ex.DIMENSIONS["phase"].extract(s["NCT90000006"])[0]
    assert missing.label == ex.MISSING_LABEL and missing.evidence[0].derived


def test_country_is_distinct_per_trial_with_first_location_as_evidence():
    vals = ex.DIMENSIONS["country"].extract(by_id()["NCT90000001"])
    assert sorted(v.label for v in vals) == ["Germany", "United States"]
    us = next(v for v in vals if v.label == "United States")
    assert us.evidence[0].path.endswith("locations[0].country")
    assert len(us.evidence) == 1  # identical excerpt from a second US site is de-duplicated by value


def test_drug_dimension_normalises_and_excludes_placebo():
    labels = [v.label for v in ex.DIMENSIONS["drug"].extract(by_id()["NCT90000001"])]
    assert labels == ["Pembrolizumab"]
    combo = ex.DIMENSIONS["drug"].extract(by_id()["NCT90000002"])
    assert [v.key for v in combo] == ["pembrolizumab", "lenvatinib"]
    # both names cite the same raw intervention string, verbatim
    assert combo[0].evidence[0].excerpt == "Pembrolizumab + Lenvatinib (E7080)"


def test_drug_dimension_ignores_non_drug_interventions():
    assert ex.DIMENSIONS["drug"].extract(by_id()["NCT90000005"]) == []


def test_drug_name_normaliser_cases():
    assert ex.drug_names("Nivolumab Injection") == [("nivolumab", "Nivolumab")]
    assert ex.drug_names("Carboplatin AUC 5") == [("carboplatin", "Carboplatin")]
    assert ex.drug_names("Gemcitabine and Cisplatin") == [("gemcitabine", "Gemcitabine"), ("cisplatin", "Cisplatin")]
    assert ex.drug_names("Saline placebo") == []


def test_time_buckets_respect_date_precision():
    s = by_id()
    assert ex.time_bucket(s["NCT90000001"], "start_date", "quarter")[0] == "2015-Q2"
    assert ex.time_bucket(s["NCT90000002"], "start_date", "month")[0] == "2016-02"
    assert ex.time_bucket(s["NCT90000006"], "start_date", "year")[0] == "2019"
    assert ex.time_bucket(s["NCT90000006"], "start_date", "month") is None  # year-only date
    assert ex.time_bucket(s["NCT90000008"], "start_date", "year") is None  # no start date


def test_bucket_range_fills_gaps():
    assert ex.bucket_range("2015", "2018", "year") == ["2015", "2016", "2017", "2018"]
    assert ex.bucket_range("2019-Q3", "2020-Q2", "quarter") == ["2019-Q3", "2019-Q4", "2020-Q1", "2020-Q2"]
    assert ex.bucket_range("2019-11", "2020-02", "month") == ["2019-11", "2019-12", "2020-01", "2020-02"]


def test_numeric_fields():
    s = by_id()
    v, ev = ex.numeric(s["NCT90000001"], "enrollment")
    assert v == 800 and ev[0].excerpt == "800" and ev[1].excerpt == "ACTUAL"
    months, ev = ex.numeric(s["NCT90000001"], "duration_months")
    assert 60 < months < 61 and all(e.derived for e in ev)
    assert ex.numeric(s["NCT90000001"], "num_sites")[0] == 3
    assert ex.numeric(s["NCT90000001"], "num_countries")[0] == 2
    assert ex.numeric(s["NCT90000008"], "enrollment") is None
    assert ex.numeric(s["NCT90000006"], "duration_months") is None


def test_match_evidence_finds_literal_occurrences_including_other_names():
    s = by_id()["NCT90000001"]
    ev = ex.match_evidence(s, {"intervention": "keytruda"})
    assert ev and ev[0].path.endswith("otherNames[1]") and ev[0].excerpt == "Keytruda"
    assert ex.match_evidence(s, {"intervention": "nivolumab"}) == []


def test_match_evidence_splits_boolean_terms():
    s = by_id()["NCT90000001"]
    ev = ex.match_evidence(s, {"term": "pembrolizumab AND (advanced melanoma)"})
    assert [e.excerpt for e in ev] == ["Pembrolizumab in Advanced Melanoma"]  # both operands hit the title once
    ev = ex.match_evidence(s, {"term": "keytruda AND melanoma"})
    assert [e.path.rsplit(".", 1)[-1] for e in ev] == ["briefTitle"]  # keytruda only in otherNames (not a term path)


def test_investigator_names_strip_degrees():
    vals = ex.DIMENSIONS["investigator"].extract(by_id()["NCT90000001"])
    assert vals[0].label == "Jane Doe" and vals[0].evidence[0].excerpt == "Jane Doe, MD"


def test_has_results_evidence_is_boolean_text():
    v = ex.DIMENSIONS["has_results"].extract(by_id()["NCT90000001"])[0]
    assert v.label == "Results posted" and v.evidence[0].excerpt == "true"


def test_missing_enum_value_gets_explicit_bucket():
    s = study("NCT9", "x", allocation=None)
    v = ex.DIMENSIONS["allocation"].extract(s)[0]
    assert v.label == ex.MISSING_LABEL
