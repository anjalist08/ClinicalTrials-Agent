/* Demo UI controller: builds the request, calls POST /v1/query, renders, and shows citations. */
(function () {
  const $ = (s) => document.querySelector(s);
  const EXAMPLES = [
    "How has the number of trials for pembrolizumab changed per year since 2015?",
    "Compare phases for trials involving nivolumab vs pembrolizumab",
    "Which countries have the most recruiting trials for multiple myeloma?",
    "Show a network of sponsors and drugs for glioblastoma trials",
    "Which drugs frequently co-occur in combination studies for pancreatic cancer?",
    "What is the distribution of enrollment sizes for phase 3 type 2 diabetes trials?",
    "Breakdown of Alzheimer's disease trials by phase and status",
    "Show a world map of hepatitis C trials",
    "How many recruiting trials are there for ALS?",
    "Show the 15 largest completed COVID-19 vaccine trials",
  ];

  const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  const list = (s) => s.split(",").map((x) => x.trim()).filter(Boolean);

  function buildRequest(form) {
    const f = new FormData(form);
    const req = { query: f.get("query").trim() };
    for (const k of ["drug_name", "condition", "sponsor", "country", "visualization_type", "citation_mode", "planner"]) {
      const v = (f.get(k) || "").trim();
      if (v) req[k] = v;
    }
    for (const k of ["start_year", "end_year", "max_records"]) if (f.get(k)) req[k] = Number(f.get(k));
    if (f.get("trial_phase")) req.trial_phase = list(f.get("trial_phase"));
    if (f.get("overall_status")) req.overall_status = list(f.get("overall_status"));
    if (f.get("compare_field") && f.get("compare_values")) req.compare = { field: f.get("compare_field"), values: list(f.get("compare_values")) };
    return req;
  }

  function setStatus(text, isError) {
    const el = $("#status");
    el.hidden = !text;
    el.className = "status" + (isError ? " error" : "");
    el.textContent = text || "";
  }

  function showTab(name) {
    document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
    document.querySelectorAll("[data-pane]").forEach((p) => (p.hidden = p.dataset.pane !== name));
  }

  function evList(evs) {
    return `<ul>${evs.map((e) => `<li><code>${esc(e.path)}</code> = “${esc(e.excerpt)}”${e.derived ? " <span class='muted'>(derived)</span>" : ""}</li>`).join("")}</ul>`;
  }

  let currentEncoding = {};

  function showCitations(datum) {
    const box = $("#citations");
    if (!datum) return;
    // describe the datum with the encoded fields only (renderers may add internal fields)
    const seen = new Set();
    const fields = Object.entries(currentEncoding)
      .filter(([name]) => !(currentEncoding.x2 && (name === "x" || name === "x2"))) // histograms: the bin label says it
      .map(([, ch]) => ch)
      .filter((ch) => !seen.has(ch.field) && seen.add(ch.field));
    const label = fields
      .filter((ch) => datum[ch.field] !== undefined && datum[ch.field] !== null && typeof datum[ch.field] !== "object")
      .map((ch) => `${esc(ch.title)}: <b>${esc(ch.type === "temporal" && typeof datum.period === "string" ? datum.period : datum[ch.field])}</b>`)
      .join(" · ");
    const cits = datum.citations || [];
    let html = `<div class="sel-head">${label}</div>`;
    html += `<p class="muted">${datum.citation_count ?? cits.length} supporting trial(s)${cits.length < (datum.citation_count || 0) ? `, showing ${cits.length}` : ""}.</p>`;
    html += cits
      .map((c) => `<div class="cite">
          <a href="${esc(c.url)}" target="_blank" rel="noopener"><b>${esc(c.nct_id)}</b></a>
          · <a href="${esc(c.api_url)}" target="_blank" rel="noopener" class="muted">API record</a>
          <div class="title">${esc(c.brief_title)}</div>
          <div class="label">Evidence for this datum</div>${evList(c.evidence)}
          ${c.match_evidence && c.match_evidence.length ? `<div class="label">Matches search term</div>${evList(c.match_evidence)}` : ""}
        </div>`)
      .join("");
    if (!cits.length && (datum.citation_count || 0) === 0) html += '<p class="muted">No trials in this bucket.</p>';
    box.innerHTML = html;
  }

  function renderDetails(body) {
    const m = body.meta;
    const items = [
      ["Interpretation", esc(m.interpretation)],
      ["Why this chart", esc(m.visualization_rationale)],
      ["Planner", `${esc(m.planner.mode)}${m.planner.model ? ` (${esc(m.planner.model)}, ${m.planner.attempts} attempt(s))` : ""}${m.planner.fallback_reason ? ` — fallback: ${esc(m.planner.fallback_reason)}` : ""}`],
      ["Counting rule", esc(m.counting_rule)],
      ["Data completeness", `${m.data_completeness.records_analyzed.toLocaleString()} records analysed of ${m.data_completeness.total_matching != null ? m.data_completeness.total_matching.toLocaleString() : "unknown"} matching (${m.data_completeness.complete ? "complete" : "partial"})`],
      ["ClinicalTrials.gov requests", m.api_requests.map((r) => `${r.group ? `<b>${esc(r.group)}</b>: ` : ""}<a href="${esc(r.url)}" target="_blank" rel="noopener"><code>${esc(r.url)}</code></a> — ${r.retrieved} retrieved / ${r.total_count ?? "?"} total${r.fallbacks && r.fallbacks.length ? ` (fallbacks: ${esc(r.fallbacks.join(", "))})` : ""}`).join("<br>")],
      ["Assumptions", (m.assumptions || []).map(esc).join("<br>") || "—"],
      ["Data snapshot", `API ${esc(m.source.api_version || "?")}, data timestamp ${esc(m.source.data_timestamp || "?")}, retrieved ${esc(m.source.retrieved_at)}`],
      ["Timing", Object.entries(m.timing_ms).map(([k, v]) => `${k} ${v} ms`).join(" · ")],
    ];
    $("#details").innerHTML = items.map(([k, v]) => `<dt>${k}</dt><dd>${v}</dd>`).join("");
  }

  async function run(evt) {
    if (evt) evt.preventDefault();
    const req = buildRequest($("#form"));
    $("#run").disabled = true;
    setStatus("Planning the query and fetching ClinicalTrials.gov records…");
    try {
      const resp = await fetch("/v1/query", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(req) });
      const body = await resp.json();
      if (!resp.ok) throw new Error(typeof body.detail === "string" ? body.detail : JSON.stringify(body.detail || body));
      setStatus("");
      $("#result").hidden = false;
      $("#vis-title").textContent = body.visualization.title;
      $("#vis-subtitle").textContent = body.visualization.subtitle || "";
      $("#json").textContent = JSON.stringify(body, null, 2);
      $("#citations").innerHTML = '<span class="muted">Click a bar, point, cell, node or edge to see the trial records that support it.</span>';
      const notes = [...body.meta.warnings.map((w) => ["warn", w])];
      $("#warnings").innerHTML = notes.map(([c, w]) => `<li class="${c}">${esc(w)}</li>`).join("");
      currentEncoding = body.visualization.encoding;
      renderDetails(body);
      showTab("chart");
      await window.renderVisualization($("#chart"), body.visualization, body.meta, showCitations);
    } catch (err) {
      setStatus(`Error: ${err.message}`, true);
    } finally {
      $("#run").disabled = false;
    }
  }

  document.addEventListener("DOMContentLoaded", async () => {
    const chips = $("#examples");
    for (const q of EXAMPLES) {
      const b = document.createElement("button");
      b.type = "button";
      b.textContent = q;
      b.addEventListener("click", () => { $("#query").value = q; run(); });
      chips.appendChild(b);
    }
    $("#form").addEventListener("submit", run);
    document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));
    try {
      const h = await (await fetch("/health")).json();
      $("#health").textContent = h.planner === "llm" ? `planner: LLM (${h.model})` : "planner: keyword rules (no LLM key)";
    } catch (_) { /* health is informative only */ }
  });
})();
