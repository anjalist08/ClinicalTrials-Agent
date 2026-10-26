"""Command-line entry point: ``python -m app.cli "question" [--drug-name X ...]``.

Prints the JSON response (same as ``POST /v1/query``) to stdout or ``--out``.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from .config import Settings
from .ctgov.client import CTGovClient, ResponseCache
from .pipeline import Engine
from .planner import Planner
from .schemas.request import QueryRequest


def parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("query")
    p.add_argument("--drug-name")
    p.add_argument("--condition")
    p.add_argument("--sponsor")
    p.add_argument("--country")
    p.add_argument("--keyword")
    p.add_argument("--phase", action="append", dest="trial_phase")
    p.add_argument("--status", action="append", dest="overall_status")
    p.add_argument("--start-year", type=int)
    p.add_argument("--end-year", type=int)
    p.add_argument("--compare-field", choices=["drug", "condition", "sponsor", "country", "term"])
    p.add_argument("--compare", nargs="+", help="values to compare (with --compare-field)")
    p.add_argument("--viz", dest="visualization_type")
    p.add_argument("--top-n", type=int)
    p.add_argument("--max-records", type=int)
    p.add_argument("--citations", dest="citation_mode", choices=["off", "summary", "full"], default="summary")
    p.add_argument("--planner", choices=["auto", "llm", "rules"], default="auto")
    p.add_argument("--plan-only", action="store_true", help="print the validated plan without fetching data")
    p.add_argument("--out", type=Path)
    return p.parse_args(argv)


def build_request(ns: argparse.Namespace) -> QueryRequest:
    fields = {k: v for k, v in vars(ns).items() if v is not None and k not in {"out", "plan_only", "compare", "compare_field"}}
    if ns.compare:
        fields["compare"] = {"field": ns.compare_field or "term", "values": ns.compare}
    return QueryRequest.model_validate(fields)


async def main(argv: list[str]) -> int:
    ns = parse_args(argv)
    settings = Settings.from_env()
    req = build_request(ns)
    client = CTGovClient(settings.ctgov_base_url, cache=ResponseCache(settings.cache_ttl_s, settings.cache_dir))
    engine = Engine(settings, client, Planner(settings))
    try:
        if ns.plan_only:
            outcome = await engine.plan(req)
            payload = {"plan": outcome.plan.model_dump(), "planner": outcome.info.model_dump(), "warnings": outcome.warnings, "notes": outcome.notes}
        else:
            payload = (await engine.run(req)).model_dump(mode="json", exclude_none=True)
    finally:
        await client.aclose()
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    if ns.out:
        ns.out.write_text(text + "\n")
        print(f"wrote {ns.out}", file=sys.stderr)
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main(sys.argv[1:])))
