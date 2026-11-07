// WP F. League page `/league` (PLAN §6.14, R-UI-1, R-LG-*): the latest league report from /api/league/report.
// The report is aggregates only (R-LG-6): scores, calibration bins, gate rates, label/outcome counts, bankrolls.
import {
  calibrationPoints, fmtCi, fmtNum, fmtPct, fmtUsd, getJSON, h, methodRows, poll, render,
} from "./prm.js";

const $ = (x) => document.getElementById(x);
const MONO = { family: "'JetBrains Mono', ui-monospace, Menlo, monospace" };
const GRID = "#2a2a3a";
const TICK = "#8d93b5";
const TRADER_COLORS = ["#7d8dff", "#f0e43a", "#c58cff", "#4fd1c5", "#ff9f43", "#ff7eb6"];
const DEFAULT_GATE = 0.9;
let view = "all";
let report = null;
let charts = {};

for (const [id, v] of [["seg-all", "all"], ["seg-post", "post_warmup"]]) {
  $(id).addEventListener("click", () => {
    view = v;
    $("seg-all").setAttribute("aria-pressed", String(v === "all"));
    $("seg-post").setAttribute("aria-pressed", String(v !== "all"));
    if (report) renderScores(report);
  });
}

function table(head, rows, rowClass = () => "") {
  return h("table", {},
    h("thead", {}, h("tr", {}, head.map((c) => h("th", {}, c)))),
    h("tbody", {}, rows.map((r, i) => h("tr", { class: rowClass(i) || null }, r.map((c) => h("td", {}, c ?? "—"))))));
}

function kpi(label, value, sub) {
  return h("div", { class: "card kpi" }, h("div", { class: "label" }, label), h("div", { class: "value" }, value),
    sub ? h("div", { class: "sub" }, sub) : null);
}

function renderSummary(r) {
  const p = r.params || {};
  const bits = [`${r.n_markets} mutants of ${p.target ?? "the target"}`, `${r.n_scored} scored`,
    `beliefs: ${r.beliefs}`, p.bug_fraction != null ? `bug fraction ${p.bug_fraction}` : null,
    p.seed != null ? `seed ${p.seed}` : null];
  $("summary").textContent = bits.filter(Boolean).join(" · ");
  const all = r.scores.all;
  const gate = (r.gate_economics.thresholds || []).find((t) => t.threshold === DEFAULT_GATE);
  const delta = all.market.brier != null && all.average.brier != null ? all.average.brier - all.market.brier : null;
  render($("kpis"),
    kpi("Market Brier", fmtNum(all.market.brier), `95% CI ${fmtCi(all.market.brier_ci)} · n = ${all.market.n}`),
    kpi("vs. simple average", delta == null ? "—" : `${delta >= 0 ? "−" : "+"}${fmtNum(Math.abs(delta))}`,
      delta == null ? "" : delta >= 0 ? "market is better (lower)" : "average is better (lower)"),
    kpi("Auto-merged at 0.90", gate ? fmtPct(gate.auto_pct) : "—", gate ? `n = ${gate.n_auto} of ${gate.n}` : ""),
    kpi("Escaped defects", gate ? fmtPct(gate.escaped_defect_rate) : "—", "bad PRs among auto-merged"),
    kpi("$ / market", fmtUsd(r.cost.per_market_usd), `${fmtUsd(r.cost.total_usd)} total`));
}

function renderScores(r) {
  const scores = r.scores[view] || {};
  const rows = methodRows(scores);
  if (!rows.some((x) => x.n > 0)) {
    return render($("scores"), h("p", { class: "empty" }, view === "post_warmup"
      ? `No markets after the warm-up: this run has ${r.n_markets}, and the first ${r.warmup} are skipped. Run more to see it.`
      : "No scored markets in this run."));
  }
  const maxBrier = Math.max(0.3, ...rows.flatMap((x) => (x.brierCi || []).concat(x.brier ?? 0)));
  const bar = (row) => {
    if (row.brier == null) return "—";
    const pct = (v) => `${Math.min(100, (v / maxBrier) * 100).toFixed(1)}%`;
    const ci = row.brierCi || [row.brier, row.brier];
    const range = h("span", { class: "ci-range" });
    range.style.left = pct(ci[0]);
    range.style.width = `calc(${pct(ci[1])} - ${pct(ci[0])})`;
    const dot = h("span", { class: "ci-dot" });
    dot.style.left = pct(row.brier);
    return h("span", { class: "ci-bar", title: `${fmtNum(row.brier)} ${fmtCi(row.brierCi)}` }, range, dot);
  };
  render($("scores"), table(
    ["method", "Brier", "95% CI", "", "log loss", "n", "note"],
    rows.map((x) => [x.label, fmtNum(x.brier), fmtCi(x.brierCi), bar(x), fmtNum(x.logLoss), String(x.n), x.note || ""]),
    (i) => (rows[i].key === "market" ? "hl" : ""),
  ), view === "post_warmup" ? h("p", { class: "muted small" }, `Skips the first ${r.warmup} markets, while bankrolls settle.`) : null);
}

function renderGate(r) {
  const rows = r.gate_economics.thresholds || [];
  render($("gate"), table(
    ["auto-merge at", "auto-merged", "escaped defects", "rejected", "false rejects"],
    rows.map((t) => [`p ≥ ${t.threshold.toFixed(2)}`, `${fmtPct(t.auto_pct)} (${t.n_auto})`, fmtPct(t.escaped_defect_rate),
      String(t.n_rejected), fmtPct(t.false_reject_rate)]),
    (i) => (rows[i].threshold === DEFAULT_GATE ? "hl" : ""),
  ));
  const e = r.gate_economics.engine || {};
  $("engine").textContent = `What the engine did (n = ${e.n ?? 0}): ${fmtPct(e.auto_merged_pct)} auto-merged, `
    + `${fmtPct(e.escalated_pct)} went to escalation, ${fmtPct(e.no_consensus_pct)} ended "no consensus", `
    + `${fmtPct(e.void_pct)} voided. Escaped defect = a bug mutant that was auto-merged; false reject = a benign one that wasn't.`;
}

function renderConfusion(r) {
  const kinds = Object.keys(r.confusion || {}).sort();
  render($("confusion"), kinds.length
    ? table(["label \\ outcome", "YES", "NO", "error"], kinds.map((k) => {
      const c = r.confusion[k];
      return [k, String(c.YES ?? 0), String(c.NO ?? 0), String(c.error ?? 0)];
    }))
    : h("p", { class: "muted" }, "No counts."));
  render($("cost"), h("dl", { class: "kv" },
    h("dt", {}, "total"), h("dd", {}, fmtUsd(r.cost.total_usd)),
    h("dt", {}, "per market"), h("dd", {}, fmtUsd(r.cost.per_market_usd)),
    h("dt", {}, "LLM calls this run"), h("dd", {}, String(r.cost.provider_calls_this_run ?? 0)),
    h("dt", {}, "beliefs"), h("dd", {}, String(r.beliefs))));
}

/** Axis styling; `unit` pins both axes to [0, 1] (probabilities) with a linear x axis. */
function axes(xTitle, yTitle, unit = false) {
  const ax = (title) => ({
    title: { display: true, text: title, color: TICK, font: MONO }, ticks: { color: TICK, font: MONO }, grid: { color: GRID },
    ...(unit ? { min: 0, max: 1 } : {}),
  });
  return { x: { ...ax(xTitle), ...(unit ? { type: "linear" } : {}) }, y: ax(yTitle) };
}

function upsert(key, canvas, config) {
  if (typeof window.Chart !== "function") return;
  if (charts[key]) {
    charts[key].data = config.data;
    charts[key].update("none");
  } else {
    charts[key] = new window.Chart(canvas, config);
  }
}

function renderCharts(r) {
  const pts = calibrationPoints(r.calibration);
  upsert("cal", $("calibration"), {
    type: "bubble",
    data: { datasets: [
      { type: "line", data: [{ x: 0, y: 0 }, { x: 1, y: 1 }], borderColor: TICK, borderDash: [6, 6], borderWidth: 1, pointRadius: 0 },
      { data: pts, backgroundColor: "rgba(31, 59, 255, 0.55)", borderColor: "#7d8dff" },
    ] },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: { legend: { display: false }, tooltip: { filter: (c) => c.datasetIndex === 1, callbacks: {
        label: (c) => `price ${fmtNum(c.raw.x, 2)} → observed ${fmtNum(c.raw.y, 2)} (n=${c.raw.n})`,
      } } },
      scales: axes("mean market price", "observed YES rate", true),
    },
  });
  const names = Object.keys(r.bankrolls || {}).sort();
  const len = Math.max(0, ...names.map((n) => r.bankrolls[n].length));
  upsert("bank", $("bankrolls"), {
    type: "line",
    data: {
      labels: Array.from({ length: len }, (_, i) => String(i + 1)),
      datasets: names.map((n, i) => ({ label: n, data: r.bankrolls[n], borderColor: TRADER_COLORS[i % TRADER_COLORS.length],
        backgroundColor: TRADER_COLORS[i % TRADER_COLORS.length], borderWidth: 2, pointRadius: 0, tension: 0 })),
    },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false, interaction: { mode: "index", intersect: false },
      plugins: { legend: { labels: { color: "#e8ecff", font: MONO, boxWidth: 12 } } },
      scales: axes("market #", "bankroll"),
    },
  });
  document.fonts?.ready.then(() => Object.values(charts).forEach((c) => c.update("none")));
}

async function refresh() {
  let r;
  try {
    r = await getJSON("/api/league/report");
  } catch (err) {
    if (err.status !== 404) throw err;
    $("report").hidden = true;
    $("empty").hidden = false;
    render($("empty"), h("div", { class: "empty" }, "No league run yet. Run ", h("code", {}, "make league-sample"),
      " (or ", h("code", {}, "prm league --target PATH --n 120"), ") and this page fills in."));
    return;
  }
  report = r;
  $("empty").hidden = true;
  $("report").hidden = false;
  renderSummary(r);
  renderScores(r);
  renderGate(r);
  renderConfusion(r);
  renderCharts(r);
}

poll(refresh, 10000, "updated");
