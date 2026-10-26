/* Reference renderer for the visualization spec (see docs/SCHEMA.md).
 *
 * It deliberately uses ONLY the documented contract: `visualization.type` picks
 * the renderer, `visualization.encoding` maps channels to data fields, and
 * `meta.rendering` supplies orientation / stacking / units. If this file needed
 * to special-case a query, the schema would be incomplete.
 *
 * renderVisualization(el, vis, meta, onSelect) -> Promise
 *   onSelect(datum) is called with the clicked row / node / edge (which carries
 *   `citations` and `citation_count`).
 */
(function () {
  const WORLD = "/static/vendor/world-110m.json"; // vega-datasets@2.8.0, vendored

  function vlChannel(ch, extra = {}) {
    if (!ch) return undefined;
    const enc = { field: ch.field, type: ch.type, title: ch.title };
    if (Array.isArray(ch.sort)) enc.sort = ch.sort;
    else if (ch.sort === "data") enc.sort = null;
    else if (ch.sort === "ascending" || ch.sort === "descending") enc.sort = ch.sort;
    if (ch.time_unit) enc.timeUnit = ch.time_unit === "quarter" ? "yearquarter" : ch.time_unit === "month" ? "yearmonth" : "year";
    if (ch.scale && ch.scale !== "linear") enc.scale = { type: ch.scale };
    if (ch.format && (ch.type === "temporal" || ch.type === "quantitative")) {
      enc.axis = { format: ch.format };
      if (ch.format === "d") enc.axis.tickMinStep = 1; // integer counts: no repeated "1, 1" ticks
      enc.legend = { format: ch.format };
    }
    return Object.assign(enc, extra);
  }

  function tooltipFor(enc) {
    const t = [];
    for (const [name, ch] of Object.entries(enc)) {
      if (["tooltip", "detail"].includes(name)) continue;
      const tt = { field: ch.field, type: ch.type, title: ch.title };
      if (ch.type === "temporal" && ch.format) tt.format = ch.format;
      t.push(tt);
    }
    if (enc.tooltip) t.unshift({ field: enc.tooltip.field, type: "nominal", title: enc.tooltip.title });
    if (enc.detail) t.unshift({ field: enc.detail.field, type: "nominal", title: enc.detail.title });
    t.push({ field: "citation_count", type: "quantitative", title: "Supporting trials" });
    return t;
  }

  function cssVar(name) {
    return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  }

  function baseConfig() {
    const text = cssVar("--text"), muted = cssVar("--muted"), line = cssVar("--line");
    return {
      background: null,
      view: { stroke: null },
      axis: { labelColor: muted, titleColor: text, gridColor: line, domainColor: line, tickColor: line, labelLimit: 220 },
      legend: { labelColor: muted, titleColor: text, labelLimit: 220 },
      title: { color: text },
    };
  }

  function vegaSpec(vis, meta, rows) {
    const enc = vis.encoding;
    const r = meta.rendering || {};
    const common = {
      $schema: "https://vega.github.io/schema/vega-lite/v5.json",
      width: "container",
      height: 380,
      data: { values: rows },
      config: baseConfig(),
    };
    const tooltip = tooltipFor(enc);
    switch (vis.type) {
      case "bar_chart":
      case "grouped_bar_chart":
      case "stacked_bar_chart": {
        const horizontal = r.orientation === "horizontal";
        const n = new Set(rows.map((d) => d[(horizontal ? enc.y : enc.x).field])).size;
        const e = { x: vlChannel(enc.x), y: vlChannel(enc.y), tooltip };
        if (!horizontal) e.x.axis = { labelAngle: n <= 8 ? 0 : -40 };
        if (enc.color) {
          e.color = vlChannel(enc.color);
          if (vis.type === "grouped_bar_chart") e[horizontal ? "yOffset" : "xOffset"] = { field: enc.color.field, sort: enc.color.sort };
        }
        if (horizontal) common.height = Math.max(220, n * (enc.color && vis.type === "grouped_bar_chart" ? 34 : 22));
        return { ...common, mark: { type: "bar", tooltip: true, cursor: "pointer" }, encoding: e };
      }
      case "heatmap":
        return {
          ...common,
          height: Math.max(220, new Set(rows.map((d) => d[enc.y.field])).size * 26),
          mark: { type: "rect", cursor: "pointer" },
          encoding: { x: vlChannel(enc.x), y: vlChannel(enc.y), color: vlChannel(enc.color, { scale: { scheme: "blues" } }), tooltip },
        };
      case "time_series": {
        const e = { x: vlChannel(enc.x), y: vlChannel(enc.y), tooltip };
        if (enc.color) e.color = vlChannel(enc.color);
        return {
          ...common,
          layer: [
            // projected (future) periods are drawn as faded points only, never joined by the line
            { transform: [{ filter: "!datum.is_projected" }], mark: { type: "line", interpolate: "monotone" }, encoding: { x: e.x, y: e.y, color: e.color } },
            {
              mark: { type: "point", filled: true, size: 55, cursor: "pointer" },
              encoding: { ...e, opacity: { condition: { test: "datum.is_projected", value: 0.35 }, value: 1 } },
            },
          ],
        };
      }
      case "histogram": {
        // the `label` channel gives a readable ordinal axis (incl. the open-ended overflow bin)
        const e = { x: vlChannel(enc.label, { sort: null, axis: { labelAngle: -40 } }), y: vlChannel(enc.y), tooltip };
        if (enc.color) {
          e.color = vlChannel(enc.color);
          e.xOffset = { field: enc.color.field };
        }
        return { ...common, mark: { type: "bar", cursor: "pointer" }, encoding: e };
      }
      case "scatter_plot": {
        const e = { x: vlChannel(enc.x), y: vlChannel(enc.y), tooltip };
        if (enc.color) e.color = vlChannel(enc.color);
        return { ...common, mark: { type: "point", filled: true, opacity: 0.65, cursor: "pointer" }, encoding: e };
      }
      case "choropleth_map": {
        const byId = rows.map((d, i) => ({ ...d, _id: d[enc.geo.field] ? parseInt(d[enc.geo.field], 10) : null, _row: i }));
        return {
          ...common,
          height: 460,
          projection: { type: "equalEarth" },
          layer: [
            { data: { url: WORLD, format: { type: "topojson", feature: "countries" } }, mark: { type: "geoshape", fill: cssVar("--line"), stroke: cssVar("--card") } },
            {
              data: { url: WORLD, format: { type: "topojson", feature: "countries" } },
              transform: [{ lookup: "id", from: { data: { values: byId }, key: "_id", fields: [enc.label.field, enc.color.field, "_row", "citation_count"] } }, { filter: "isValid(datum._row)" }],
              mark: { type: "geoshape", stroke: cssVar("--card"), cursor: "pointer" },
              encoding: {
                color: vlChannel(enc.color, { scale: { scheme: "blues" } }),
                tooltip: [{ field: enc.label.field, title: enc.label.title }, { field: enc.color.field, title: enc.color.title, type: "quantitative" }],
              },
            },
          ],
        };
      }
    }
    return null;
  }

  async function renderVega(el, vis, meta, onSelect) {
    const rows = vis.data;
    const spec = vegaSpec(vis, meta, rows);
    const res = await vegaEmbed(el, spec, { actions: { export: true, source: false, compiled: false, editor: false }, renderer: "svg" });
    res.view.addEventListener("click", (_evt, item) => {
      if (!item || !item.datum) return;
      const d = item.datum;
      if (vis.type === "choropleth_map") {
        if (d._row != null) onSelect(rows[d._row]);
      } else if (d.citation_count !== undefined) {
        onSelect(d);
      }
    });
    return res;
  }

  const PALETTE = ["#2f6fd6", "#e07a1f", "#2a9d6f", "#b5479b", "#7a6ff0", "#c24d4d"];

  function renderNetwork(el, vis, meta, onSelect) {
    const enc = vis.encoding;
    const { nodes, edges } = vis.data;
    el.innerHTML = "";
    const box = document.createElement("div");
    box.className = "network";
    el.appendChild(box);
    const types = (enc.node_color.sort || []).concat([...new Set(nodes.map((n) => n[enc.node_color.field]))]);
    const colorOf = (t) => PALETTE[[...new Set(types)].indexOf(t) % PALETTE.length];
    const maxN = Math.max(1, ...nodes.map((n) => n[enc.node_size.field]));
    const maxE = Math.max(1, ...edges.map((e) => e[enc.edge_weight.field]));
    const text = cssVar("--text");
    const visNodes = nodes.map((n) => ({
      id: n[enc.node_id.field],
      label: n[enc.node_label.field],
      value: n[enc.node_size.field],
      size: 8 + 30 * Math.sqrt(n[enc.node_size.field] / maxN),
      color: colorOf(n[enc.node_color.field]),
      title: `${n[enc.node_label.field]} (${n[enc.node_color.field]}): ${n[enc.node_size.field]} trials`,
      shape: n[enc.node_color.field] === (enc.node_color.sort || [])[0] ? "dot" : "diamond",
      font: { color: text, size: 22, strokeWidth: 4, strokeColor: cssVar("--card") },
    }));
    const visEdges = edges.map((e, i) => ({
      id: i,
      from: e[enc.edge_source.field],
      to: e[enc.edge_target.field],
      width: 1 + 7 * (e[enc.edge_weight.field] / maxE),
      title: `${e[enc.edge_weight.field]} shared trials`,
      color: { color: cssVar("--muted"), opacity: 0.45 },
    }));
    const VisLib = window.vis; // the `vis` parameter (the spec) shadows the vis-network global
    const net = new VisLib.Network(
      box,
      { nodes: new VisLib.DataSet(visNodes), edges: new VisLib.DataSet(visEdges) },
      { physics: { solver: "forceAtlas2Based", stabilization: { iterations: 250 } }, interaction: { hover: true, tooltipDelay: 120 }, nodes: { shape: "dot" } }
    );
    net.on("click", (params) => {
      if (params.nodes.length) onSelect(nodes.find((n) => n[enc.node_id.field] === params.nodes[0]));
      else if (params.edges.length) onSelect(edges[params.edges[0]]);
    });
    const legend = document.createElement("p");
    legend.className = "muted";
    legend.innerHTML = [...new Set(types)]
      .map((t) => `<span style="color:${colorOf(t)}">●</span> ${t}`)
      .join(" &nbsp; ") + ` &nbsp;·&nbsp; node size = ${enc.node_size.title.toLowerCase()}, edge width = ${enc.edge_weight.title.toLowerCase()}`;
    el.appendChild(legend);
  }

  function esc(s) {
    return String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
  }

  function renderTable(el, vis, meta, onSelect) {
    const cols = Object.values(vis.encoding);
    const head = cols.map((c) => `<th>${esc(c.title)}${c.unit && c.unit !== "date" ? ` (${esc(c.unit)})` : ""}</th>`).join("");
    const body = vis.data
      .map((row, i) => `<tr data-i="${i}">` + cols.map((c) => {
        const v = row[c.field];
        if (c.field === "nct_id") return `<td><a href="https://clinicaltrials.gov/study/${esc(v)}" target="_blank" rel="noopener">${esc(v)}</a></td>`;
        return `<td>${typeof v === "number" ? v.toLocaleString() : esc(v)}</td>`;
      }).join("") + "</tr>")
      .join("");
    el.innerHTML = `<table class="data"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
    el.querySelectorAll("tbody tr").forEach((tr) => tr.addEventListener("click", () => onSelect(vis.data[+tr.dataset.i])));
  }

  function renderKpi(el, vis, meta, onSelect) {
    const v = vis.encoding.value, l = vis.encoding.label;
    el.innerHTML = `<div class="kpis">${vis.data
      .map((row, i) => `<div class="kpi" data-i="${i}"><div class="value">${Number(row[v.field]).toLocaleString()}</div><div class="muted">${esc(row[l.field])} · ${esc(v.unit || "")}</div></div>`)
      .join("")}</div>`;
    el.querySelectorAll(".kpi").forEach((k) => k.addEventListener("click", () => onSelect(vis.data[+k.dataset.i])));
  }

  window.renderVisualization = async function (el, vis, meta, onSelect) {
    el.innerHTML = "";
    const empty = Array.isArray(vis.data) ? vis.data.length === 0 : vis.data.nodes.length === 0;
    if (empty) {
      el.innerHTML = '<p class="muted">No data to display for this query (see notes below).</p>';
      return;
    }
    if (vis.type === "network_graph") return renderNetwork(el, vis, meta, onSelect);
    if (vis.type === "table") return renderTable(el, vis, meta, onSelect);
    if (vis.type === "kpi") return renderKpi(el, vis, meta, onSelect);
    return renderVega(el, vis, meta, onSelect);
  };
  window.vegaSpecFor = vegaSpec; // exposed for debugging / tests
})();
