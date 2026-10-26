"""Citation audit: verify that every citation in a response is literally supported by its source record.

Used by the test-suite (against fixture records) and by ``scripts/run_examples.py``
(against records re-fetched independently from the live API). Checks:

* each cited NCT ID exists in the source records;
* each non-derived evidence excerpt equals the value at its JSON path;
* each derived evidence path resolves (``[*]`` paths: the parent list exists);
* each match-evidence excerpt equals the value at its path and contains a search term;
* ``citation_count`` >= number of citations, and, in ``full`` mode, equals it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from .ctgov.extract import get_path


@dataclass
class AuditReport:
    data_items: int = 0
    citations: int = 0
    evidence: int = 0
    failures: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.failures

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "data_items": self.data_items,
            "citations_checked": self.citations,
            "evidence_checked": self.evidence,
            "failures": self.failures[:50],
        }


def iter_data_items(visualization: dict[str, Any]) -> Iterable[tuple[str, dict[str, Any]]]:
    data = visualization["data"]
    if isinstance(data, dict):  # network
        for n in data.get("nodes", []):
            yield f"node {n.get('id')}", n
        for e in data.get("edges", []):
            yield f"edge {e.get('source')}--{e.get('target')}", e
    else:
        for i, row in enumerate(data):
            yield f"row {i}", row


def _stringify(value: Any) -> str:
    if isinstance(value, bool):
        return str(value).lower()
    return str(value)


def audit(
    response: dict[str, Any],
    studies_by_id: dict[str, dict[str, Any]],
    *,
    search_terms: list[str] | None = None,
    nct_ids: set[str] | None = None,
) -> AuditReport:
    """``nct_ids`` restricts checking to those trials (used for sampled live audits)."""
    report = AuditReport()
    mode = response["meta"]["citations"].get("mode", "summary")
    terms = [t.lower() for t in (search_terms or [])]
    for where, item in iter_data_items(response["visualization"]):
        report.data_items += 1
        cits = item.get("citations") or []
        if "citation_count" in item and item["citation_count"] < len(cits):
            report.failures.append(f"{where}: citation_count {item['citation_count']} < {len(cits)} citations")
        if mode == "full" and "citation_count" in item and item["citation_count"] != len(cits):
            report.failures.append(f"{where}: full mode but {len(cits)} of {item['citation_count']} cited")
        for c in cits:
            nct = c["nct_id"]
            if nct_ids is not None and nct not in nct_ids:
                continue
            report.citations += 1
            study = studies_by_id.get(nct)
            if study is None:
                report.failures.append(f"{where}: cited {nct} not among source records")
                continue
            if not c["url"].endswith(nct):
                report.failures.append(f"{where}: url {c['url']} does not point at {nct}")
            for ev in c.get("evidence", []):
                report.evidence += 1
                path, excerpt = ev["path"], ev["excerpt"]
                if ev.get("derived"):
                    base = path.split("[*]")[0]
                    if "(field absent)" in excerpt:
                        if get_path(study, path) not in (None, [], ""):
                            report.failures.append(f"{where}/{nct}: {path} claimed absent but present")
                    elif get_path(study, base) is None:
                        report.failures.append(f"{where}/{nct}: derived path {path} does not resolve")
                    continue
                actual = get_path(study, path)
                if actual is None or _stringify(actual) != excerpt:
                    report.failures.append(f"{where}/{nct}: {path} = {actual!r}, excerpt says {excerpt!r}")
            for ev in c.get("match_evidence", []):
                report.evidence += 1
                actual = get_path(study, ev["path"])
                if actual != ev["excerpt"]:
                    report.failures.append(f"{where}/{nct}: match {ev['path']} = {actual!r} != {ev['excerpt']!r}")
                elif terms and not any(t in ev["excerpt"].lower() for t in terms):
                    report.failures.append(f"{where}/{nct}: match excerpt {ev['excerpt']!r} contains no search term")
    return report
