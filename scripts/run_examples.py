"""Run the example queries against the LIVE ClinicalTrials.gov API and record the outputs.

For each example this writes, under ``examples/``:

* ``NN_<slug>.request.json``  - the request body sent to ``POST /v1/query``
* ``NN_<slug>.response.json`` - the exact response produced
* ``NN_<slug>.audit.json``    - an independent citation audit: a sample of cited
  trials is re-fetched one by one from ``/api/v2/studies/{nct_id}`` (full record,
  no cache, no field projection) and every evidence excerpt is checked against it

and regenerates ``examples/README.md`` (an index table).

Usage:  python scripts/run_examples.py [--only 1,3] [--audit-sample 25]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.audit import audit, iter_data_items  # noqa: E402
from app.config import Settings  # noqa: E402
from app.ctgov.client import CTGovClient, ResponseCache  # noqa: E402
from app.pipeline import Engine  # noqa: E402
from app.planner import Planner  # noqa: E402
from app.schemas.request import QueryRequest  # noqa: E402

EXAMPLES: list[dict] = [
    # the example request from the assignment brief: a structured field resolves "this drug"
    {"query": "How has the number of trials for this drug changed over time?", "drug_name": "Pembrolizumab"},
    {"query": "Compare phases for trials involving nivolumab vs pembrolizumab"},
    {"query": "Which countries have the most recruiting trials for multiple myeloma?"},
    {"query": "Show a network of sponsors and drugs for glioblastoma trials", "trial_phase": ["Phase 2", "Phase 3"]},
    {"query": "Which drugs frequently co-occur in combination studies for pancreatic cancer?", "max_records": 10000},
    {"query": "What is the distribution of enrollment sizes for phase 3 type 2 diabetes trials?"},
    {"query": "Breakdown of Alzheimer's disease trials by phase and status", "visualization_type": "stacked_bar_chart",
     "max_records": 10000},
]

OUT = ROOT / "examples"


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")[:60]


def search_terms(response: dict) -> list[str]:
    plan = response["meta"]["plan"]
    terms = [v for v in plan["filters"].values() if isinstance(v, str)]
    for g in plan.get("groups", []):
        terms += [v for v in g["filters"].values() if isinstance(v, str)]
    return terms


async def independent_audit(response: dict, sample: int, seed: int = 7) -> dict:
    cited = sorted({c["nct_id"] for _, item in iter_data_items(response["visualization"]) for c in item.get("citations", [])})
    rng = random.Random(seed)
    chosen = sorted(rng.sample(cited, min(sample, len(cited))))
    client = CTGovClient(Settings.from_env().ctgov_base_url, cache=None)  # fresh, uncached
    try:
        records = {}
        for nct in chosen:
            records[nct] = await client.get_study(nct)
    finally:
        await client.aclose()
    report = audit(response, records, search_terms=search_terms(response), nct_ids=set(chosen))
    return {"cited_trials": len(cited), "independently_refetched": chosen, **report.as_dict()}


def summarize(i: int, req: dict, resp: dict, aud: dict, name: str) -> str:
    v, m = resp["visualization"], resp["meta"]
    data = v["data"]
    size = f"{len(data['nodes'])} nodes / {len(data['edges'])} edges" if isinstance(data, dict) else f"{len(data)} rows"
    dc = m["data_completeness"]
    total = f"{dc['records_analyzed']:,} / {dc['total_matching']:,}" if dc["total_matching"] is not None else f"{dc['records_analyzed']:,} / ?"
    planner = m["planner"]["mode"] + (f" ({m['planner']['model']})" if m["planner"].get("model") else "")
    audit_txt = f"{'✅' if aud['ok'] else '❌'} {aud['citations_checked']} cites / {aud['evidence_checked']} excerpts"
    return (
        f"| {i} | {req['query']}{' + ' + ', '.join(f'`{k}`' for k in req if k != 'query') if len(req) > 1 else ''} "
        f"| `{v['type']}` | {size} | {total} | {planner} | {audit_txt} | [response]({name}.response.json) · [audit]({name}.audit.json) |"
    )


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", help="comma-separated 1-based indexes")
    ap.add_argument("--audit-sample", type=int, default=25)
    args = ap.parse_args()
    only = {int(x) for x in args.only.split(",")} if args.only else None

    settings = Settings.from_env()
    client = CTGovClient(settings.ctgov_base_url, cache=ResponseCache(settings.cache_ttl_s, settings.cache_dir))
    engine = Engine(settings, client, Planner(settings))
    OUT.mkdir(exist_ok=True)
    rows = []
    try:
        for i, body in enumerate(EXAMPLES, start=1):
            if only and i not in only:
                continue
            name = f"{i:02d}_{slug(body['query'])}"
            print(f"[{i}] {body['query']}", file=sys.stderr)
            resp = (await engine.run(QueryRequest.model_validate(body))).model_dump(mode="json", exclude_none=True)
            aud = await independent_audit(resp, args.audit_sample)
            (OUT / f"{name}.request.json").write_text(json.dumps(body, indent=2, ensure_ascii=False) + "\n")
            (OUT / f"{name}.response.json").write_text(json.dumps(resp, indent=2, ensure_ascii=False) + "\n")
            (OUT / f"{name}.audit.json").write_text(json.dumps(aud, indent=2) + "\n")
            print(f"    -> {resp['visualization']['type']}: {resp['visualization']['title']} | audit ok={aud['ok']}", file=sys.stderr)
            rows.append(summarize(i, body, resp, aud, name))
    finally:
        await client.aclose()

    if not only:
        header = (
            "# Example runs (live ClinicalTrials.gov data)\n\n"
            "Generated by `python scripts/run_examples.py`. Each response is the exact output of `POST /v1/query`; "
            "each audit re-fetches a random sample of cited trials individually from the API and checks every "
            "evidence excerpt against the full record.\n\n"
            "| # | Request | Type | Size | Records analysed / matching | Planner | Citation audit | Files |\n"
            "|---|---|---|---|---|---|---|---|\n"
        )
        (OUT / "README.md").write_text(header + "\n".join(rows) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
