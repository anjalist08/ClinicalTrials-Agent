# ClinicalTrials.gov Query-to-Visualization Agent

A backend service that turns natural-language questions about clinical trials into **frontend-ready
visualization specifications** computed from the [ClinicalTrials.gov Data API v2](https://clinicaltrials.gov/data-api/api).
Every value is backed by **deep citations**: the NCT ID, its links and the exact field/value in the API
record behind each bar, point, cell, node and edge.

```
"Compare phases for trials involving nivolumab vs pembrolizumab"
        │
        ▼  LLM planner (structured outputs) → grounding → validation
{analysis: distribution, dimension: phase, groups: [nivolumab, pembrolizumab]}
        │
        ▼  2 API queries (query.intr=…), paging, retries, local filter verification
raw study records ──► deterministic counting with evidence ──► grouped_bar_chart spec + citations + meta
```

- **8 analysis types → 11 visualization types**: bar / grouped / stacked bars, heatmap, time series,
  histogram, scatter, choropleth map, network graph (bipartite and co-occurrence), ranked table, KPI.
- **One coherent pipeline**: every question is a typed `QueryPlan`; no per-question code paths.
- **LLM never touches the numbers**: it only fills a constrained plan; counts come from deterministic code.
- **Deep citations** with JSON paths + exact excerpts, independently auditable against the live API.
- **Small demo UI** (`/`) that renders any response using only the documented schema; click a datum to see its citations.
- **156 automated tests** (fixture-backed API, fake LLM, headless-browser UI tests) plus opt-in live tests.

---

## Contents
1. [Quick start](#1-quick-start)
2. [Architecture](#2-architecture)
3. [Query & visualization coverage](#3-query--visualization-coverage)
4. [Request / response schema](#4-request--response-schema)
5. [Deep citations](#5-deep-citations-bonus)
6. [Key design decisions & trade-offs](#6-key-design-decisions--trade-offs)
7. [AI / agent design: avoiding hallucination](#7-ai--agent-design-avoiding-hallucination)
8. [How correctness was validated](#8-how-correctness-was-validated)
9. [Example runs](#9-example-runs)
10. [Demo](#10-demo)
11. [Limitations & what I would improve](#11-limitations--what-i-would-improve)
12. [Tools used, and what was designed vs generated](#12-tools-used-and-what-was-designed-vs-generated)
13. [Project layout](#13-project-layout)

---

## 1. Quick start

Requirements: Python ≥ 3.10, internet access to `clinicaltrials.gov` (and `api.openai.com` for the LLM planner).

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt            # runtime
pip install -r requirements-dev.txt        # + tests / lint (optional)

cp .env.example .env                       # then set OPENAI_API_KEY (optional, see below)
uvicorn app.main:app --reload --port 8000
```

- Demo UI: <http://localhost:8000/> · OpenAPI docs: <http://localhost:8000/docs> · JSON Schemas: `GET /v1/schema`
- Without `OPENAI_API_KEY` the service still works using the deterministic keyword planner (`meta.planner.mode = "rules"`).

| Env var | Default | Purpose |
|---|---|---|
| `OPENAI_API_KEY` | – | Enables the LLM planner |
| `OPENAI_MODEL` | `gpt-4.1-mini` | Any allowed model (`gpt-4o-2024-08-06`, `gpt-4.1`, `gpt-5-mini`, …) |
| `PLANNER` | `auto` | `auto` (LLM if key, else rules) · `llm` · `rules` |
| `MAX_RECORDS` | `1000` | Records fetched per dataset (request can raise to 10 000) |
| `CTGOV_CACHE_DIR` | – | Optional on-disk cache of API responses (reproducible demos) |
| `CTGOV_BASE_URL` | `https://clinicaltrials.gov/api/v2` | |

**Call it**

```bash
curl -s localhost:8000/v1/query -H 'content-type: application/json' -d '{
  "query": "How has the number of trials for this drug changed over time?",
  "drug_name": "Pembrolizumab"
}' | jq '.visualization.type, .visualization.data[0], .meta.data_completeness'

# dry run: see the plan and the exact API parameters, no data fetched
curl -s localhost:8000/v1/plan -H 'content-type: application/json' \
  -d '{"query":"Which drugs frequently co-occur in combination studies for pancreatic cancer?"}' | jq
```

**CLI** (same output as the API):

```bash
python -m app.cli "Compare phases for trials involving nivolumab vs pembrolizumab" --out out.json
python -m app.cli "trials per year" --drug-name pembrolizumab --start-year 2015 --citations full
python -m app.cli "Show a network of sponsors and drugs" --condition glioblastoma --plan-only
```

**Tests**

```bash
pytest                       # 156 offline tests (fake ClinicalTrials.gov + fake LLM + headless UI)
pytest -m live               # opt-in: real ClinicalTrials.gov (and OpenAI if configured)
python scripts/run_examples.py   # regenerate examples/ from live data, with independent citation audits
ruff check .
```

The UI tests need Chromium for Playwright (`python -m playwright install chromium`); they are skipped if it is unavailable.

**Docker**: `docker build -t ctgov-agent . && docker run -p 8000:8000 --env-file .env ctgov-agent`

---

## 2. Architecture

```
app/
├── main.py              FastAPI: /v1/query, /v1/plan, /v1/schema, /health, UI
├── pipeline.py          Engine: plan → fetch → verify → aggregate → build → meta
├── planner/
│   ├── llm.py           OpenAI Structured Outputs (strict JSON schema) + 1 repair round
│   ├── rules.py         deterministic keyword planner (fallback / no key)
│   ├── grounding.py     drop filters not traceable to the user's input
│   └── validate.py      hard checks, normalisation, structured-field overrides
├── ctgov/
│   ├── client.py        async API client: paging, retries, 400-fallback ladder, cache
│   ├── query_builder.py Filters → query.cond / query.intr / filter.advanced (Essie) …; local re-verification
│   └── extract.py       evidence-carrying field extractors over raw API JSON (22 dimensions, 6 numeric fields)
├── analytics/
│   ├── aggregate.py     buckets, time bins, cross-tabs, histograms, networks (each bucket keeps its evidence)
│   └── citations.py     buckets → citations (summary / full / off)
├── viz/
│   ├── builder.py       one builder per analysis → visualization spec + rendering hints + rationale
│   └── geo.py           country → ISO 3166 codes for maps
├── schemas/             request, plan (the LLM's output), response, enums
├── audit.py             citation auditor (used by tests and by the live example runner)
└── cli.py
web/                     demo UI (vanilla JS; Vega-Lite + vis-network vendored)
tests/                   156 offline + 5 live tests; fixtures/factory.py builds API-v2-shaped synthetic records
scripts/run_examples.py  live example generator + independent citation audit
docs/SCHEMA.md           full request/response contract
examples/                example requests, responses and audits
```

**Stages**

| # | Stage | Deterministic? | Guard-rails |
|---|---|---|---|
| 1 | **Plan**: question (+ structured fields) → `QueryPlan` | LLM (or rules) | strict JSON schema of closed enums; Pydantic + semantic validation; one repair round with the errors; fallback to rules |
| 2 | **Ground & merge** | ✅ | ungrounded filters dropped (with warnings); structured request fields override; defaults & equivalences normalised |
| 3 | **Fetch**: one API query per dataset (base, or one per comparison group), concurrently | ✅ | paging to `max_records`, `countTotal`, retries/backoff, `fields` projection, 400-fallback ladder, TTL cache, zero-result recovery |
| 4 | **Verify** every record against enum/range filters locally | ✅ | non-matching records dropped and reported |
| 5 | **Aggregate** with per-trial evidence | ✅ | documented counting rules; explicit zeros; shared bins/categories across groups |
| 6 | **Build** spec + citations + meta | ✅ | chart choice rationale; completeness, warnings, API URLs, data timestamp |

---

## 3. Query & visualization coverage

| Question class (appendix examples and more) | Plan | Visualization |
|---|---|---|
| "How has the number of trials for [drug] changed per year since 2015?" · "How many trials started each year for [condition]?" | `time_trend` (start / completion / first-posted date; year / quarter / month) | `time_series` (optionally one line per group or per phase/status/…) |
| "How are [condition] trials distributed across phases?" · "most common intervention types / sponsors / conditions / drugs …" | `distribution` over 22 dimensions | `bar_chart` (auto horizontal for long labels) |
| "Compare phases for trials involving Drug A vs Drug B" · "Compare sponsor categories for asthma vs COPD" (counts or %) | `distribution` + `groups` | `grouped_bar_chart` / `stacked_bar_chart` |
| "Breakdown of [condition] trials by phase and status" | `cross_tab` | `stacked_bar_chart` or `heatmap` |
| "Which countries have the most recruiting trials for [condition]?" · "show a world map …" | `distribution` by `country` | `bar_chart` or `choropleth_map` |
| "Show a network of sponsors ↔ drugs for [condition]" · investigators ↔ sites, conditions ↔ drugs … | `network` (bipartite) | `network_graph` |
| "Which drugs frequently co-occur in combination studies?" | `network` (co-occurrence) | `network_graph` |
| "Distribution of enrollment sizes / trial durations / number of sites" | `histogram` | `histogram` (shared bins, overflow bin for long tails) |
| "Enrollment vs duration for [condition] trials" | `scatter` | `scatter_plot` (one point per trial) |
| "Show the 15 largest / longest / most recent trials for …" | `trial_list` | `table` |
| "How many recruiting trials are there for ALS in Germany?" | `total` | `kpi` (exact API `countTotal`) |

Dimensions: phase, overall_status, study_type, intervention_type, drug, intervention, condition, sponsor,
sponsor_class, collaborator, country, facility, investigator, allocation, intervention_model,
primary_purpose, masking, sex, age_group, has_results, start_year, completion_year.
Numeric fields: enrollment, duration_months, num_sites, num_countries, num_interventions, start_date.
Network entities: any of drug, intervention, intervention_type, condition, sponsor, collaborator,
sponsor_class, country, facility, investigator, phase — in any pairing.

Filters (from text or structured fields, freely combined): condition, intervention, sponsor, free-text
term, country, phases, statuses, study type, intervention types, sponsor classes, start-year range.

---

## 4. Request / response schema

Full contract, per-type channel tables and error codes: **[docs/SCHEMA.md](docs/SCHEMA.md)**
(machine-readable: `GET /v1/schema`). In short:

**Request** — `query` (required) + optional authoritative fields: `drug_name`, `condition`, `sponsor`,
`country`, `keyword`, `trial_phase[]`, `overall_status[]`, `study_type`, `intervention_type[]`,
`sponsor_class[]`, `start_year`, `end_year`, `compare {field, values[2-6]}`, `visualization_type`,
`top_n`, `max_records`, `citation_mode (off|summary|full)`, `max_citations_per_datum`, `planner`.
Phases/statuses are normalised (`"phase III"` → `PHASE3`); unknown fields → 422.

**Response**

```jsonc
{
  "visualization": {
    "type": "grouped_bar_chart",
    "title": "Trials by phase: nivolumab vs pembrolizumab",
    "encoding": {
      "x":     {"field": "phase", "type": "ordinal", "title": "Phase", "sort": ["Early Phase 1", "Phase 1", "Phase 1/2", "Phase 2", "..."]},
      "y":     {"field": "trial_count", "type": "quantitative", "title": "Number of trials", "format": "d", "unit": "trials"},
      "color": {"field": "group", "type": "nominal", "title": "Comparison group", "sort": ["nivolumab", "pembrolizumab"]}
    },
    "data": [
      {"phase": "Phase 2", "trial_count": 412, "group": "nivolumab", "citation_count": 412,
       "citations": [{"nct_id": "NCT0…", "url": "https://clinicaltrials.gov/study/NCT0…",
                      "api_url": "https://clinicaltrials.gov/api/v2/studies/NCT0…", "brief_title": "…",
                      "evidence": [{"path": "protocolSection.designModule.phases[0]", "excerpt": "PHASE2"}],
                      "match_evidence": [{"path": "protocolSection.armsInterventionsModule.interventions[0].name", "excerpt": "Nivolumab"}]}]}
    ]
  },
  "meta": {
    "interpretation": "…", "visualization_rationale": "…", "plan": {…}, "planner": {"mode": "llm", "model": "gpt-4.1-mini", "attempts": 1},
    "filters_applied": {…}, "groups": [{"label": "nivolumab", "filters": {"intervention": "nivolumab"}}, …],
    "counting_rule": "Each trial is counted at most once per category.",
    "rendering": {"orientation": "vertical", "stacked": false, "sort": "natural category order", "units": {"trial_count": "trials"}, "grouping": "comparison group"},
    "data_completeness": {"total_matching": 2875, "records_analyzed": 2000, "complete": false, "note": "…"},
    "api_requests": [{"group": "nivolumab", "url": "https://clinicaltrials.gov/api/v2/studies?query.intr=nivolumab&…", "total_count": 1123, "retrieved": 1000, "pages": 1, "fallbacks": []}],
    "assumptions": […], "warnings": […],
    "source": {"name": "ClinicalTrials.gov", "api_version": "…", "data_timestamp": "…", "retrieved_at": "…"},
    "timing_ms": {"plan": 812, "fetch": 2140, "aggregate": 35, "total": 2987}
  }
}
```
*(illustrative values — see [examples/](examples/) for real outputs)*

Renderer rules worth knowing: category order is given explicitly in `encoding.*.sort`; horizontal bars
already have the category on `y`; time series contain explicit zero periods and flag `is_partial` /
`is_projected`; histogram `bin_end = null` is the overflow bin; network `data` is `{nodes, edges}`.

---

## 5. Deep citations (bonus)

Every datum carries `citation_count` (all contributing trials) and `citations` (first *k* in `summary`
mode, all in `full` mode). Each citation has:

- `nct_id`, `url` (study page), `api_url` (raw record), `brief_title`;
- `evidence[]`: **the JSON path and exact value** in the API record that put the trial in this datum —
  e.g. `protocolSection.designModule.phases[0] = "PHASE3"`, `…locations[4].country = "Germany"`,
  `…startDateStruct.date = "2017-03-15"`; both endpoints for a network edge; both coordinates for a
  scatter point; `derived: true` for computed values (durations, site counts, "field absent");
- `match_evidence[]`: where the search term literally appears (e.g. an intervention's `otherNames[1] = "Keytruda"`).

How it works: extractors in `app/ctgov/extract.py` return `(value, [Evidence(path, excerpt)])` instead of
bare values, aggregation buckets keep the evidence per trial, and the citation factory serialises it.
Because paths point into the unmodified API JSON, citations are **machine-verifiable**:
`app/audit.py` resolves every path and checks the excerpt, and `scripts/run_examples.py` re-fetches a random
sample of cited trials one by one from `/api/v2/studies/{nct_id}` (no cache, full record) and audits them.

---

## 6. Key design decisions & trade-offs

| Decision | Why | Trade-off |
|---|---|---|
| **Plan DSL instead of free-form agent / code generation.** The LLM emits a `QueryPlan` (analysis, dimensions, filters, groups) from closed vocabularies. | One coherent path covers all question classes; plans are inspectable (`/v1/plan`), testable and reproducible; no generated code to execute. | Questions outside the DSL (e.g. outcome measures, eligibility text mining) are answered with the nearest supported analysis or not at all. Extending = add a dimension extractor or a builder. |
| **Numbers computed locally from raw records, not by the LLM and not by separate count queries.** | Makes deep citations possible (we know *which* trials made each number) and keeps all datums consistent with each other. | For very large result sets the analysis covers the first `max_records` records (default 1000, up to 10 000); this is always flagged in `data_completeness` + warnings. KPIs use the API's exact `countTotal`. |
| **Use the API's search areas for text filters** (`query.cond`, `query.intr`, `query.spons`, `query.locn`, `query.term`). | The API expands synonyms/MeSH (NSCLC, MK-3475 → pembrolizumab), which a local matcher cannot. | Text matching is trusted to the API; we can only show literal `match_evidence` where it exists. |
| **Enum/range filters sent to the API *and* re-verified locally.** | A malformed or ignored server-side filter can never leak wrong trials into a chart. If the API rejects the Essie expression, the client retries without it and the filter is applied locally. | Slight redundancy; with the fallback the reported total becomes an upper bound (flagged). |
| **Counting rule: distinct trials per bucket.** A trial with 40 US sites counts once for the US. Multi-valued fields (countries, conditions, drugs) can put a trial in several buckets — stated in `meta.counting_rule`. | Matches how people read "trials per country". | Totals across buckets can exceed the number of trials (explicitly documented). |
| **Combined phases kept as their own category** (`Phase 1/2`, `Phase 2/3`), matching ClinicalTrials.gov conventions. Phase *filters* use API semantics (a Phase 2/3 trial matches "Phase 3"). | Avoids double-counting in distributions while staying consistent with the API in filters. | Users expecting "Phase 2" to include 1/2 trials see them separately. |
| **Drug normalisation** for drug dimensions/networks: strip doses, routes, parentheticals; split `A + B`; drop placebo/standard-of-care. Label = most common surface form. | Makes drug networks meaningful (`Pembrolizumab 200 mg IV` = `pembrolizumab`). | No synonym dictionary (MK-3475 stays separate from pembrolizumab) — see limitations. |
| **Visualization chosen from the analysis, not by the LLM.** Rules: categorical → bars (horizontal if many/long labels); groups → grouped; two dims → stacked or heatmap (>8 series); time → line; numeric → histogram; two numerics → scatter; relationships → network; single number → KPI; specific trials → table. `visualization_type` is a validated preference. | Deterministic, explainable (`meta.visualization_rationale`), cannot produce a chart that does not fit the data. | Less "creative" chart choice. |
| **Spec format: channel → field mapping close to Vega-Lite**, with explicit sort lists, units, formats and orientation. | Trivial to render with Vega-Lite/ECharts/Plotly; the demo renderer is ~250 lines and has no per-query logic. | A Vega-Lite spec could have been emitted directly, but that would couple the API to one library. |
| **Async I/O, concurrent group queries, TTL cache (memory + optional disk).** | Comparisons fetch in parallel; repeated demo queries are instant and polite to the public API. | In-process cache only (no Redis). |
| **Explicit honesty in `meta`**: completeness, warnings (dropped filters, fallbacks, excluded records, projected dates), API URLs, data timestamp, timing. | Real-world API data is messy; the frontend can show caveats instead of silently wrong charts. | Larger responses. |

**Handling real-world API data**: partial dates (`YYYY`, `YYYY-MM`) bucketed only at the precision they support;
missing fields get an explicit *Not specified* bucket (enum dimensions) or are excluded with a counted warning
(numeric/time); future (anticipated) dates flagged `is_projected`; long-tailed enrollment folded into an overflow
bin; skewed axes get a `symlog` scale hint; country spellings mapped to ISO codes; repeated values within a record
(e.g. many sites in one country) de-duplicated; 429/5xx retried with backoff honouring `Retry-After`;
zero results on an area-specific search retried once as full-text (and reported).

---

## 7. AI / agent design: avoiding hallucination

The LLM is used for the one step that needs language understanding (mapping a question to a plan) and is
fenced in on every side:

1. **Constrained decoding** — OpenAI Structured Outputs with `strict: true` and the `QueryPlan` JSON schema;
   every dimension, phase, status and chart preference is an enum, so invalid values cannot be produced.
2. **No data in, no data out** — the model sees only the question, structured fields and today's date; it
   never sees or produces trial data, counts or NCT IDs.
3. **Validation + repair** — Pydantic parsing, then semantic checks (e.g. `cross_tab` needs two different
   dimensions, comparisons need 2–6 grounded groups, year ranges valid). Violations are sent back once as a
   repair prompt; if still invalid, the deterministic planner takes over (`meta.planner.fallback_reason`).
4. **Grounding** — every free-text filter must occur in the question/structured fields (exact, token-wise,
   or fuzzy ≥ 0.85 for typos); enum filters (phases, statuses, years, sponsor classes, …) require a lexical
   cue in the question. This removes the most common LLM failure: plausible constraints the user never asked
   for (e.g. adding `RECRUITING`, or "correcting" a drug name). The prompt also tells the model to copy entity
   names verbatim because the API does synonym expansion.
5. **Structured fields win** — explicit request fields override anything inferred.
6. **Deterministic fallback** — the keyword planner (`rules.py`) covers every appendix question class and
   uses the same validation, so the service degrades gracefully without a key or during an outage.
7. **Transparency** — the executed plan, interpretation, assumptions and warnings are returned with every answer.

Model choice: `gpt-4.1-mini` by default (fast, cheap, reliable structured outputs); any model from the allowed
list can be configured (`gpt-5*` models are called without `temperature`, which they don't accept).

---

## 8. How correctness was validated

- **Unit tests on fixture records shaped exactly like API v2 JSON** (`tests/fixtures/factory.py`: partial
  dates, Phase 1/2 combinations, observational studies without phases, placebo arms, combination
  interventions, multi-site/multi-country trials, missing enrollment …): extractors, normalisers, bucket
  counting, ordering, histogram binning invariants (every value in exactly one half-open bin), network
  edge weights, citation modes.
- **Planner tests**: every question class from the assignment appendix (and more) maps to the expected plan;
  grounding drops hallucinated filters but keeps legitimate ones; validation rejects malformed plans;
  structured-field overrides; LLM planner tested with a scripted fake OpenAI client (strict schema has no
  unsupported keywords, repair loop, give-up, fallback, forced-LLM errors, `gpt-5` temperature handling).
- **API-client tests** against an `httpx.MockTransport` fake of ClinicalTrials.gov: paging tokens, retries on
  503, `fields`/`filter.advanced` 400-fallback ladder, disk cache.
- **End-to-end HTTP tests** for every visualization type: response validates against the schema, every encoded
  field exists in the data, and **every citation is audited** (`app/audit.py`: excerpt == value at path).
  Behavioural checks: explicit zero years, exact KPI counts, percent sums to 100 per group, incomplete-data
  flags, zero-result recovery, server-side filter rejection, empty results.
- **Headless-browser tests** (Playwright/Chromium) render every visualization type in the demo UI with no
  JavaScript errors and check that clicking a datum shows its citations.
- **Live validation** (`pytest -m live`, `scripts/run_examples.py`): checks that the real API accepts the field
  projection and Essie filters without fallbacks, that enum filters hold on every returned record, and that cited
  trials **re-fetched independently** from `/api/v2/studies/{nct_id}` match every excerpt (results in
  `examples/*.audit.json`).
- `ruff` clean.

---

## 9. Example runs

Real outputs from the live API are in **[examples/](examples/)**: for each query, the request, the exact JSON
response, and an independent citation audit. Index and summary: [examples/README.md](examples/README.md).
Regenerate with `python scripts/run_examples.py`.

| # | Query | Visualization |
|---|---|---|
| 1 | "How has the number of trials for this drug changed over time?" + `drug_name: Pembrolizumab` (the brief's example) | `time_series` |
| 2 | "Compare phases for trials involving nivolumab vs pembrolizumab" | `grouped_bar_chart` |
| 3 | "Which countries have the most recruiting trials for multiple myeloma?" | `bar_chart` |
| 4 | "Show a network of sponsors and drugs for glioblastoma trials" + `trial_phase: [2, 3]` | `network_graph` (bipartite) |
| 5 | "Which drugs frequently co-occur in combination studies for pancreatic cancer?" | `network_graph` (co-occurrence) |
| 6 | "What is the distribution of enrollment sizes for phase 3 type 2 diabetes trials?" | `histogram` |
| 7 | "Breakdown of Alzheimer's disease trials by phase and status" | `stacked_bar_chart` |

---

## 10. Demo

A **small UI** is served at `/` by the same app (`web/`): type a question (or click an example), optionally
fill structured fields, and the response is rendered — bars/lines/heatmaps/histograms/scatter/maps with
Vega-Lite, networks with vis-network, tables and KPI cards in plain HTML. **Click any datum** to see its
citations (NCT links, API record links, evidence paths and excerpts). Other tabs show how the question was
answered (plan, planner, counting rule, completeness, exact API URLs, data snapshot) and the raw JSON.

The renderer (`web/renderers.js`) uses only the documented schema — it is effectively the reference
implementation of [docs/SCHEMA.md](docs/SCHEMA.md). Libraries are vendored (`web/vendor/`), so the demo works
without CDN access.

---

## 11. Limitations & what I would improve

- **Sampling for very large result sets.** Aggregates cover up to `max_records` per dataset (default 1000,
  max 10 000) in API relevance order; flagged in `data_completeness`. Improvement: for single-valued
  dimensions (phase, status, start year) compute exact counts with per-bucket `countTotal` queries and keep the
  sampled records for citations; or stream all pages with a background job.
- **Entity normalisation.** Drug names are normalised lexically; synonyms/codes (MK-3475 vs pembrolizumab) are not
  merged, and sponsor names are not de-duplicated across subsidiaries. Improvement: map interventions to
  RxNorm/MeSH (the API's `derivedSection.interventionBrowseModule` is a start) and sponsors to a canonical list.
- **Investigator networks** use `overallOfficials` (site-level investigators are rarely published in the API).
- **Rules planner** is intentionally simple (keyword lexicon + residual-term search); it is a fallback, not a
  replacement for the LLM on complex phrasing.
- **Out-of-DSL questions** (outcomes, adverse events, eligibility criteria text, results data) are not supported.
  The DSL is designed to grow: a new dimension is one extractor; a new analysis is one builder.
- **Semantics**: "trials for X" means "API search match for X", not "X is the primary intervention".
  Improvement: optional stricter matching using `match_evidence` (e.g. only count trials where X is an intervention arm).
- **Ops**: in-process cache only; no auth/rate limiting on our endpoint; no streaming progress for slow queries.
  Would add Redis caching, request coalescing, OpenTelemetry tracing and an evaluation set of question → expected
  plan pairs run against each LLM model to choose the best cost/accuracy trade-off.

---

## 12. Tools used, and what was designed vs generated

**Tools**: Python 3, FastAPI, httpx (async), Pydantic v2, OpenAI Python SDK (Structured Outputs),
pycountry, pytest / pytest-asyncio, Playwright (headless Chromium) for UI tests, ruff; Vega-Lite and
vis-network in the demo UI. **AI assistance**: the code, tests and docs were produced with
**Claude Code** (Anthropic's AI coding agent) working from the assignment brief, then reviewed, run and
iterated on with the test-suite.

**Designed deliberately** (decisions made up-front and enforced by tests, rather than accepted from generated code):
- the plan-DSL architecture and the rule that the LLM never produces data;
- the evidence-carrying extractor design that makes citations exact and auditable, and the auditor itself;
- grounding rules for LLM output, the validation/repair/fallback chain, and "structured fields win";
- counting semantics (distinct trials per bucket, combined phases, multi-valued fields), explicit zeros,
  projected/partial periods, overflow bins, completeness reporting;
- the response contract (explicit sort orders, literal channel positions, `{nodes, edges}` for networks) and
  proving it with a renderer that has no per-query logic;
- the test strategy: API-shaped fixtures, a fake API with failure injection, a fake LLM, headless-browser
  tests, and live independent re-fetch audits.

**Generated and adapted**: most implementation code, the fixture corpus, the CSS, and first drafts of the docs were
generated, then corrected where tests or review found problems.

**Iteration log — real issues found by the tests/review and fixed during development**:
1. Rules planner: `nations?` matched inside "combi**nation**", turning a drug–drug network into drug–country;
   "percent**age**" matched the age-group lexicon → all lexicon patterns now use word boundaries (regression tests added).
2. Tokenizer emitted an empty token for a trailing `.`, producing group labels with a trailing space.
3. Multi-site trials produced dozens of identical country evidence entries → evidence de-duplicated by value.
4. Histogram computed one extra empty bin when no overflow bin was needed.
5. Pydantic defaults leaked `default` keywords into the strict OpenAI schema → stripped recursively; test asserts.
6. A test expected phase-3 filtering to exclude Phase 2/3 trials; the implementation correctly follows API
   semantics, so the test was fixed and the semantics documented.
7. Demo UI: the network renderer's parameter named `vis` shadowed the vis-network global, so networks never
   rendered — caught by the headless-browser test.
8. Self-review: the rules planner joins several search terms as `A AND (B C)`, which the grounding check then
   rejected (the literal "and" is not in the question) and `match_evidence` never matched → both now treat
   each boolean operand separately (regression tests added).
9. UI polish found from screenshots: repeated integer ticks on small counts, vertical labels for short
   categories, Vega-internal fields shown in the citation header.

---

## 13. Project layout

See [§2](#2-architecture). Supporting files: `requirements*.txt`, `pyproject.toml` (pytest/ruff config),
`.env.example`, `Dockerfile`, `docs/SCHEMA.md`, `examples/`, `web/vendor/README.md` (vendored library licenses).
