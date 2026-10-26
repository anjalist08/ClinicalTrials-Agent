# Request / response schema

Machine-readable JSON Schemas are served at `GET /v1/schema` (`request`, `response`, `plan`) and in the
OpenAPI document at `/docs`. This page is the human contract: it says exactly what a renderer must do
for each visualization type, so nothing has to be guessed.

- [Request](#request-post-v1query)
- [Response envelope](#response)
- [Channels](#channel-object)
- [Per-type contract](#per-type-contract)
- [Citations](#citations-deep-traceability)
- [Meta](#meta)
- [Errors](#errors)
- [Dry run: `POST /v1/plan`](#dry-run-post-v1plan)

---

## Request (`POST /v1/query`)

Only `query` is required. **Structured fields are authoritative**: when present they override whatever the
planner inferred from the text. Unknown fields are rejected (`422`).

| Field | Type | Req. | Default | Validation / normalisation | Meaning |
|---|---|---|---|---|---|
| `query` | string | ✅ | | 3–1000 chars, trimmed | Natural-language question |
| `drug_name` | string | | | ≤200 chars | Searched in the API *intervention* area (`query.intr`, synonym-aware) |
| `condition` | string | | | ≤200 | `query.cond` (MeSH/synonym-aware) |
| `sponsor` | string | | | ≤200 | `query.spons` (lead sponsor or collaborator) |
| `country` | string | | | ≤100 | `query.locn` — at least one site in that country |
| `keyword` | string | | | ≤200 | `query.term` — free text, all fields |
| `trial_phase` | string[] | | | Accepts `"Phase 3"`, `"phase III"`, `"3"`, `"PHASE3"`, `"early phase 1"`, `"NA"` → API enum `EARLY_PHASE1 \| PHASE1..4 \| NA` (de-duplicated) | Phase filter (a *Phase 2/3* trial matches both 2 and 3, as in the API) |
| `overall_status` | string[] | | | Case/spacing tolerant → `RECRUITING`, `NOT_YET_RECRUITING`, `ACTIVE_NOT_RECRUITING`, `COMPLETED`, `TERMINATED`, `WITHDRAWN`, `SUSPENDED`, `ENROLLING_BY_INVITATION`, … | Status filter |
| `study_type` | string | | | `INTERVENTIONAL \| OBSERVATIONAL \| EXPANDED_ACCESS` | |
| `intervention_type` | string[] | | | `DRUG, BIOLOGICAL, DEVICE, PROCEDURE, BEHAVIORAL, RADIATION, GENETIC, DIETARY_SUPPLEMENT, DIAGNOSTIC_TEST, COMBINATION_PRODUCT, OTHER` | |
| `sponsor_class` | string[] | | | `INDUSTRY, NIH, FED, OTHER_GOV, NETWORK, INDIV, OTHER, AMBIG, UNKNOWN` | Lead-sponsor class |
| `start_year` / `end_year` | int | | | 1900–2100, `start_year ≤ end_year` | Study start date range (inclusive) |
| `compare` | object | | | `{"field": "drug"\|"condition"\|"sponsor"\|"country"\|"term", "values": [2–6 distinct strings]}` | One dataset per value, rendered as series |
| `visualization_type` | string | | | one of the [types](#per-type-contract) | Preference; honoured when compatible, otherwise a note explains why not |
| `top_n` | int | | per dimension (15–30) | 1–100 | Max categories / nodes / rows |
| `max_records` | int | | `MAX_RECORDS` env (5000) | 1–10000 | Records fetched **per dataset** |
| `citation_mode` | string | | `summary` | `off \| summary \| full` | See [citations](#citations-deep-traceability) |
| `max_citations_per_datum` | int | | 3 | 1–50 | Used in `summary` mode |
| `planner` | string | | `auto` | `auto \| llm \| rules` | Force a planner (debug/eval) |

```json
{
  "query": "How has the number of trials for this drug changed over time?",
  "drug_name": "Pembrolizumab",
  "start_year": 2010,
  "citation_mode": "summary"
}
```

---

## Response

```jsonc
{
  "visualization": {
    "type": "grouped_bar_chart",          // selects the renderer
    "title": "Trials by phase: nivolumab vs pembrolizumab",
    "subtitle": "recruiting",             // human summary of filters (optional)
    "encoding": { "<channel>": Channel },  // channel -> data column
    "data": [ { ...row, "citation_count": 12, "citations": [Citation] } ]
                                           // network_graph: {"nodes": [...], "edges": [...]}
  },
  "meta": { ... }                          // see Meta
}
```

Null-valued optional fields are omitted from the JSON. Values *inside* data rows are never omitted
(e.g. `bin_end: null` is meaningful).

## Channel object

| Field | Type | Meaning |
|---|---|---|
| `field` | string | Column in each data row (or node/edge) |
| `type` | `nominal \| ordinal \| quantitative \| temporal` | Vega-Lite measurement type |
| `title` | string | Axis / legend / column title |
| `sort` | `"ascending" \| "descending" \| "data" \| string[]` | A list is the exact category order to use (it already reflects the server's ordering: natural order for ordinal dimensions such as phase, count-descending otherwise). `"data"` = keep row order |
| `format` | string | d3-format (numbers, e.g. `d`, `.1f`) or d3-time-format (dates, e.g. `%Y`) |
| `unit` | string | `trials`, `%`, `participants`, `months`, `sites`, … |
| `time_unit` | `year \| quarter \| month` | Temporal bucketing of `period_start` |
| `scale` | `linear \| log \| sqrt \| symlog` | Suggested scale (e.g. `symlog` for skewed enrollment; `sqrt` for node sizes) |

---

## Per-type contract

Every tabular row also has `citation_count` and (unless `citation_mode = off`) `citations`.
`trial_count` is always an integer count of distinct trials.

### `bar_chart`, `grouped_bar_chart`, `stacked_bar_chart`
Category counts. Positions are literal: when `meta.rendering.orientation = "horizontal"` the **category is
on `y`** and the value on `x` (the server already swapped them), so a renderer just maps channels.

| Channel | Field | Notes |
|---|---|---|
| `x` / `y` | the dimension name, e.g. `phase`, `country`, `sponsor` | `sort` = category order |
| `y` / `x` | `trial_count`, or `percent` when `plan.metric = "percent"` | |
| `color` | `group` | grouped/stacked only; `sort` = group order |

Row: `{ "<dimension>": "Phase 2", "trial_count": 41, "group"?: "nivolumab", "percent"?: 23.4, "group_total"?: 175 }`.
Grouped charts contain a row for **every** (category, group) pair, zeros included. `grouped` = side-by-side
(`xOffset`), `stacked` = stacked (`meta.rendering.stacked = true`). When `type = bar_chart` and the
question was a comparison of totals, the category field is `label`.

### `heatmap`
| Channel | Field |
|---|---|
| `x` | first dimension |
| `y` | second dimension |
| `color` | `trial_count` (or `percent` = share within the x category) |

Dense grid: one row per (x, y) cell, zeros included.

### `time_series`
| Channel | Field | Notes |
|---|---|---|
| `x` | `period_start` (ISO date, `temporal`) | `time_unit` + `format` given; `period` holds a label such as `2019`, `2019-Q2`, `2019-05` |
| `y` | `trial_count` / `percent` | |
| `color` | `group` | when comparing groups or splitting by a dimension (`series_dimension`) |

Rows are ascending by period, with **explicit zero rows** for empty periods. Flags:
`is_partial` (period contains today — incomplete) and `is_projected` (period in the future: trials with
*anticipated* dates). Renderers should de-emphasise both (the demo dims projected points).

### `histogram`
| Channel | Field | Notes |
|---|---|---|
| `x` / `x2` | `bin_start` / `bin_end` | half-open `[bin_start, bin_end)`; **`bin_end = null` marks the overflow bin** (values ≥ `bin_start`, above the 95th percentile when the tail is long; `is_overflow: true`) |
| `y` | `trial_count` / `percent` | |
| `label` | `bin_label` | ready-made ordinal label (`"0–49"`, `"≥ 1,000"`) — simplest way to render |
| `color` | `group` | comparisons; bins are shared across groups |

### `scatter_plot`
One row per trial: `{ "nct_id", "brief_title", "<x_field>", "<y_field>", "group"? }`.
Channels: `x`, `y` (quantitative, or temporal for `start_date`), `tooltip` (`brief_title`), `detail` (`nct_id`), optional `color` (`group`).
`citation_count` is 1 and the single citation carries the evidence for both coordinates.

### `choropleth_map`
| Channel | Field | Notes |
|---|---|---|
| `geo` | `iso_numeric` | ISO 3166-1 numeric = feature `id` in the world-atlas / vega-datasets `world-110m` TopoJSON |
| `label` | `country` | ClinicalTrials.gov spelling |
| `color` | `trial_count` | trials with ≥1 site in the country |

Rows also contain `iso_alpha3`. All countries are returned (no `top_n`).

### `network_graph`
`data = { "nodes": [...], "edges": [...] }`.

| Channel | Field | Applies to |
|---|---|---|
| `node_id` | `id` (`"<entity_type>:<normalised key>"`, unique) | nodes |
| `node_label` | `label` | nodes |
| `node_size` | `trial_count` (`scale: sqrt`) | nodes |
| `node_color` | `entity_type` (`sort` lists the types: source first) | nodes |
| `edge_source` / `edge_target` | `source` / `target` (node ids) | edges |
| `edge_weight` | `trial_count` — trials containing both endpoints | edges |

Nodes also carry `degree`. Equal source/target types ⇒ co-occurrence graph (e.g. drug–drug);
different types ⇒ bipartite (e.g. sponsor–drug). Every edge references existing nodes; isolated
nodes are removed. `meta.rendering.categories_total` vs `categories_shown` tells how many entities
existed before the `top_n` cut, and a warning reports any minimum edge weight applied for legibility.

### `table`
Every encoding entry is a column, **in display order**: `rank`, `nct_id`, `brief_title`, `phase`,
`overall_status`, `lead_sponsor`, and the sort field (e.g. `enrollment`). Rows are pre-sorted.

### `kpi`
A single number. Channels: `value` (`trial_count`, the API's exact `countTotal`), `label`.
With comparison groups a `bar_chart` of one count per group is returned instead.

---

## Citations (deep traceability)

```jsonc
{
  "nct_id": "NCT02142738",
  "url": "https://clinicaltrials.gov/study/NCT02142738",          // human page
  "api_url": "https://clinicaltrials.gov/api/v2/studies/NCT02142738", // raw record, for machine checks
  "brief_title": "Study of Pembrolizumab (MK-3475) Compared to Platinum-Based Chemotherapies ...",
  "evidence": [                                                   // why the trial is in THIS datum
    { "path": "protocolSection.designModule.phases[0]", "excerpt": "PHASE3" }
  ],
  "match_evidence": [                                             // where the search term occurs
    { "path": "protocolSection.armsInterventionsModule.interventions[0].name", "excerpt": "Pembrolizumab" }
  ]
}
```

- `excerpt` is the **exact value at `path`** in the API v2 study JSON (`true`/`false` for booleans).
  `derived: true` marks evidence the datum is *computed* from (a location count, a duration from two
  dates, "field absent" for a *Not specified* bucket); its path still resolves.
- Edges cite evidence for **both** endpoints in the same trial; scatter points and table rows cite every
  plotted column.
- `match_evidence` is empty when the API matched through a synonym/MeSH expansion that does not occur
  literally in the record — that is reported rather than invented.
- `citation_count` is always the full number of contributing trials. `summary` mode lists the first
  `max_citations_per_datum` (API order); `full` lists all; `off` omits `citations`.
- `meta.citations.excerpt_semantics` restates these rules in every response.

## Meta

| Field | Meaning |
|---|---|
| `query`, `interpretation` | Original question, one-sentence restatement |
| `visualization_rationale` | Why this chart type answers the question |
| `plan` | The full validated `QueryPlan` that was executed (see `GET /v1/schema` → `plan`) |
| `planner` | `{mode: llm\|rules, model, attempts, fallback_reason}` |
| `filters_applied` | Base filters; `groups` lists per-group filters |
| `counting_rule` | Exactly how numbers were computed (e.g. multi-valued fields can exceed the trial total) |
| `rendering` | `orientation`, `stacked`, `time_granularity`, `sort`, `units`, `grouping`, `value_field`, `categories_total`, `categories_shown`, `missing_values` |
| `data_completeness` | `{total_matching, records_analyzed, complete, note}` — `complete=false` means counts cover the first `max_records` records |
| `citations` | `{mode, max_per_datum, excerpt_semantics}` |
| `api_requests[]` | Every ClinicalTrials.gov search: `url` (reproducible), `total_count`, `retrieved`, `pages`, `from_cache`, `fallbacks` |
| `assumptions[]`, `warnings[]` | Judgement calls; anything that changed the answer (dropped ungrounded filters, partial data, fallbacks, excluded records) |
| `source` | `{name, api, api_version, data_timestamp, retrieved_at}` — the data snapshot |
| `timing_ms` | `plan`, `fetch`, `aggregate`, `total` |

## Errors

| Status | `error` | When |
|---|---|---|
| 422 | (FastAPI validation) | Request fails the schema above |
| 422 | `invalid_plan` | Structured fields make the plan invalid (detail lists the violations) |
| 502 | `upstream_error` | ClinicalTrials.gov unreachable after retries / unexpected status |
| 503 | `planner_unavailable` | `planner: "llm"` forced but the LLM is not configured or failed |

Body: `{"error": "...", "detail": ...}`.

## Dry run (`POST /v1/plan`)

Same request body; returns `{plan, planner, warnings, notes, api_params: [{group, params}]}` without
fetching any trial data — useful to inspect how a question is interpreted.
