"""Builders for *synthetic* study records in the ClinicalTrials.gov API v2 JSON shape.

These are test fixtures, not real trials: NCT IDs use the reserved-looking
range NCT9xxxxxxx and titles are invented. The shape (module and field names,
enum spellings, partial dates) mirrors https://clinicaltrials.gov/data-api/api
so the extractors are exercised against the same structure as live data.
"""

from __future__ import annotations

from typing import Any


def study(
    nct: str,
    title: str,
    *,
    phases: list[str] | None = None,
    status: str = "COMPLETED",
    start: str | None = "2018-03",
    primary_completion: str | None = "2020-06-15",
    first_posted: str = "2018-01-10",
    study_type: str = "INTERVENTIONAL",
    lead: tuple[str, str] = ("Merck Sharp & Dohme LLC", "INDUSTRY"),
    collaborators: list[tuple[str, str]] = (),
    conditions: list[str] = (),
    keywords: list[str] = (),
    interventions: list[tuple[str, str]] = (),  # (type, name)
    other_names: dict[str, list[str]] | None = None,
    sites: list[tuple[str, str, str]] = (),  # (facility, city, country)
    enrollment: tuple[int, str] | None = (100, "ACTUAL"),
    officials: list[str] = (),
    has_results: bool = False,
    allocation: str | None = "RANDOMIZED",
    std_ages: list[str] = ("ADULT", "OLDER_ADULT"),
    sex: str = "ALL",
) -> dict[str, Any]:
    other_names = other_names or {}
    design: dict[str, Any] = {"studyType": study_type}
    if phases is not None:
        design["phases"] = phases
    if allocation:
        design["designInfo"] = {"allocation": allocation, "primaryPurpose": "TREATMENT", "maskingInfo": {"masking": "NONE"}}
    if enrollment:
        design["enrollmentInfo"] = {"count": enrollment[0], "type": enrollment[1]}
    status_mod: dict[str, Any] = {
        "overallStatus": status,
        "studyFirstPostDateStruct": {"date": first_posted, "type": "ACTUAL"},
    }
    if start:
        status_mod["startDateStruct"] = {"date": start, "type": "ACTUAL"}
    if primary_completion:
        status_mod["primaryCompletionDateStruct"] = {"date": primary_completion, "type": "ACTUAL"}
    proto: dict[str, Any] = {
        "identificationModule": {"nctId": nct, "briefTitle": title, "officialTitle": f"A Study of {title}"},
        "statusModule": status_mod,
        "sponsorCollaboratorsModule": {
            "leadSponsor": {"name": lead[0], "class": lead[1]},
            **({"collaborators": [{"name": n, "class": c} for n, c in collaborators]} if collaborators else {}),
        },
        "conditionsModule": {"conditions": list(conditions), **({"keywords": list(keywords)} if keywords else {})},
        "designModule": design,
        "armsInterventionsModule": {
            "interventions": [
                {"type": t, "name": n, **({"otherNames": other_names[n]} if n in other_names else {})}
                for t, n in interventions
            ]
        },
        "eligibilityModule": {"sex": sex, "stdAges": list(std_ages)},
        "contactsLocationsModule": {
            **({"locations": [{"facility": f, "city": c, "country": k} for f, c, k in sites]} if sites else {}),
            **({"overallOfficials": [{"name": o, "role": "PRINCIPAL_INVESTIGATOR"} for o in officials]} if officials else {}),
        },
    }
    return {"protocolSection": proto, "hasResults": has_results}


def corpus() -> list[dict[str, Any]]:
    """Eight varied records covering the edge cases the extractors must handle."""
    return [
        study(
            "NCT90000001", "Pembrolizumab in Advanced Melanoma",
            phases=["PHASE3"], start="2015-06-01", conditions=["Melanoma"],
            interventions=[("DRUG", "Pembrolizumab 200 mg IV"), ("DRUG", "Placebo")],
            other_names={"Pembrolizumab 200 mg IV": ["MK-3475", "Keytruda"]},
            sites=[("MD Anderson", "Houston", "United States"), ("Charité", "Berlin", "Germany"),
                   ("Mayo Clinic", "Rochester", "United States")],
            enrollment=(800, "ACTUAL"), officials=["Jane Doe, MD"], has_results=True,
        ),
        study(
            "NCT90000002", "Pembrolizumab Plus Lenvatinib in Endometrial Cancer",
            phases=["PHASE1", "PHASE2"], start="2016-02", status="ACTIVE_NOT_RECRUITING",
            lead=("Eisai Inc.", "INDUSTRY"), collaborators=[("Merck Sharp & Dohme LLC", "INDUSTRY")],
            conditions=["Endometrial Cancer"],
            interventions=[("DRUG", "Pembrolizumab + Lenvatinib (E7080)")],
            sites=[("Charité", "Berlin", "Germany")], enrollment=(120, "ACTUAL"),
        ),
        study(
            "NCT90000003", "Nivolumab Versus Chemotherapy in NSCLC",
            phases=["PHASE3"], start="2016-11-20", lead=("Bristol-Myers Squibb", "INDUSTRY"),
            conditions=["Non-small Cell Lung Cancer"],
            interventions=[("BIOLOGICAL", "Nivolumab"), ("DRUG", "Docetaxel")],
            sites=[("Gustave Roussy", "Villejuif", "France")], enrollment=(582, "ACTUAL"),
            officials=["John Roe"],
        ),
        study(
            "NCT90000004", "Nivolumab and Ipilimumab in Melanoma",
            phases=["PHASE2"], start="2018-07", status="RECRUITING", lead=("Bristol-Myers Squibb", "INDUSTRY"),
            conditions=["Melanoma"], interventions=[("BIOLOGICAL", "Nivolumab"), ("BIOLOGICAL", "Ipilimumab")],
            sites=[("MD Anderson", "Houston", "United States")], enrollment=(60, "ESTIMATED"),
            primary_completion="2027-01",
        ),
        study(
            "NCT90000005", "Exercise Program for Cancer Survivors",
            phases=["NA"], start="2018-01-15", lead=("University of Oxford", "OTHER"),
            conditions=["Breast Cancer", "Fatigue"], interventions=[("BEHAVIORAL", "Aerobic exercise")],
            sites=[("John Radcliffe Hospital", "Oxford", "United Kingdom")], enrollment=(45, "ACTUAL"),
            allocation=None,
        ),
        study(
            "NCT90000006", "Registry of Immunotherapy Outcomes",
            phases=None, study_type="OBSERVATIONAL", start="2019", lead=("National Cancer Institute (NCI)", "NIH"),
            conditions=["Melanoma", "Lung Cancer"], interventions=[], sites=[], enrollment=(5000, "ESTIMATED"),
            status="RECRUITING", primary_completion=None, allocation=None,
        ),
        study(
            "NCT90000007", "Pembrolizumab in Head and Neck Cancer",
            phases=["PHASE2"], start="2020-03", conditions=["Head and Neck Squamous Cell Carcinoma"],
            interventions=[("BIOLOGICAL", "pembrolizumab"), ("RADIATION", "Radiotherapy")],
            sites=[("Princess Margaret", "Toronto", "Canada"), ("MD Anderson", "Houston", "United States")],
            enrollment=(0, "ACTUAL"), status="WITHDRAWN",
        ),
        study(
            "NCT90000008", "Lenvatinib in Thyroid Cancer",
            phases=["PHASE2", "PHASE3"], start=None, lead=("Eisai Inc.", "INDUSTRY"),
            conditions=["Thyroid Cancer"], interventions=[("DRUG", "Lenvatinib"), ("DRUG", "Placebo")],
            sites=[("Tokyo Hospital", "Tokyo", "Japan")], enrollment=None, primary_completion=None,
        ),
    ]
