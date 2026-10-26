"""Turn aggregation buckets into deep citations.

A citation names a trial (``nct_id`` + links) and lists the exact field values
from that trial's API record that put it into the datum (``evidence``), plus
where the user's search terms occur in it (``match_evidence``). Citations are
deterministic: trials are listed in the order the API returned them.
"""

from __future__ import annotations

from typing import Any

from ..ctgov import extract as ex
from .aggregate import Bucket

STUDY_URL = "https://clinicaltrials.gov/study/{}"
API_URL = "https://clinicaltrials.gov/api/v2/studies/{}"


class CitationFactory:
    def __init__(
        self,
        studies_by_id: dict[str, dict[str, Any]],
        text_filters: dict[str, str],
        mode: str = "summary",
        max_per_datum: int = 3,
    ):
        self.studies = studies_by_id
        self.text_filters = text_filters
        self.mode = mode
        self.max_per_datum = max_per_datum
        self._match_cache: dict[str, list[ex.Ev]] = {}

    def _match(self, nct: str) -> list[ex.Ev]:
        if nct not in self._match_cache:
            study = self.studies.get(nct)
            self._match_cache[nct] = ex.match_evidence(study, self.text_filters) if study and self.text_filters else []
        return self._match_cache[nct]

    def citation(self, nct: str, evidence: list[ex.Ev]) -> dict[str, Any]:
        study = self.studies.get(nct, {})
        return {
            "nct_id": nct,
            "url": STUDY_URL.format(nct),
            "api_url": API_URL.format(nct),
            "brief_title": ex.brief_title(study),
            "evidence": [_ev(e) for e in evidence],
            "match_evidence": [_ev(e) for e in self._match(nct)],
        }

    def for_bucket(self, bucket: Bucket | None) -> dict[str, Any]:
        """Fields to merge into a data row: ``citation_count`` and (unless off) ``citations``."""
        if bucket is None:
            return {"citation_count": 0, "citations": []} if self.mode != "off" else {"citation_count": 0}
        out: dict[str, Any] = {"citation_count": bucket.count}
        if self.mode == "off":
            return out
        items = list(bucket.evidence.items())
        if self.mode == "summary":
            items = items[: self.max_per_datum]
        out["citations"] = [self.citation(nct, evs) for nct, evs in items]
        return out


def _ev(e: ex.Ev) -> dict[str, Any]:
    d: dict[str, Any] = {"path": e.path, "excerpt": e.excerpt}
    if e.derived:
        d["derived"] = True
    return d
