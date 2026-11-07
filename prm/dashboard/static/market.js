// WP F. Market page `/m/{id}` (PLAN §6.14, R-UI-1): price chart, trades, beliefs (after close), gate,
// settlement counts, cost/latency, state timeline, description and diff. Polled every 2 s until terminal.
import {
  OPEN_STATES, diffLineClass, fmtMs, fmtPrice, fmtTime, fmtUsd, getJSON, githubUrl, h, marketLabel, marketStatus, parseDiff, poll, render,
} from "./prm.js";

const id = Number((location.pathname.match(/^\/m\/(\d+)/) || [])[1]);
const $ = (x) => document.getElementById(x);
const TRADER_COLORS = ["#7d8dff", "#f0e43a", "#c58cff", "#4fd1c5", "#ff9f43", "#ff7eb6"];
const DIFF_LINE_LIMIT = 1500;
const MONO = { family: "'JetBrains Mono', ui-monospace, Menlo, monospace" };
let chart = null;
let current = null; // the latest market, for the chart tooltip
let lastDiff = null;
let lastDescription = null;
let finished = false;

function table(head, rows) {
  return h("table", {},
    h("thead", {}, h("tr", {}, head.map((c) => h("th", {}, c)))),
    h("tbody", {}, rows.map((r) => h("tr", {}, r.map((c) => (c instanceof Node ? h("td", {}, c) : h("td", {}, c ?? "—")))))));
}

function dl(pairs) {
  return h("dl", { class: "kv" }, pairs.flatMap(([k, v]) => [h("dt", {}, k), h("dd", {}, v ?? "—")]));
}

function renderHero(m) {
  const st = marketStatus(m);
  const gh = githubUrl(m);
  document.title = `PRediction Market · ${marketLabel(m)}`;
  render($("hero"),
    h("div", { class: `hero-price tone-${st.tone}` }, h("span", { class: "price" }, fmtPrice(m.price)),
      h("span", { class: "muted small" }, OPEN_STATES.has(m.state) ? "live price" : "closing price")),
    h("div", { class: "hero-text" },
      h("h1", {}, marketLabel(m)),
      h("div", { class: "hero-meta" },
        h("span", { class: `badge tone-${st.tone}` }, m.state),
        m.source !== "github" ? h("span", { class: "badge" }, m.source) : null,
        h("span", { class: "gate-label" }, st.label),
        gh ? h("a", { href: gh, target: "_blank", rel: "noopener noreferrer" }, "PR on GitHub ↗") : null),
      m.status_description ? h("p", { class: "status mono" }, `pr-market: ${m.status_description}`) : null,
      st.verdict ? h("p", { class: `mono ${st.verdict.right === true ? "right" : st.verdict.right === false ? "wrong" : "muted"}` },
        st.verdict.text) : null));
}

function colorFor(names, name) {
  return TRADER_COLORS[names.indexOf(name) % TRADER_COLORS.length];
}

function renderChart(m) {
  const names = [...new Set(m.trades.map((t) => t.trader))];
  const labels = ["open", ...m.trades.map((t, i) => `${i + 1} · ${t.trader}`)];
  const data = [m.initial_price, ...m.trades.map((t) => t.price_after)];
  const points = ["#e8ecff", ...m.trades.map((t) => colorFor(names, t.trader))];
  $("chart-note").textContent = m.trades.length
    ? `${m.trades.length} trades by ${names.join(", ")}${m.escalations ? ` · ${m.escalations} escalation round(s)` : ""}`
    : "No trades yet.";
  if (typeof window.Chart !== "function") {
    $("chart-note").textContent += " (chart library unavailable: see the trade table)";
    return;
  }
  if (chart) {
    chart.data.labels = labels;
    chart.data.datasets[0].data = data;
    chart.data.datasets[0].pointBackgroundColor = points;
    chart.update("none");
    return;
  }
  chart = new window.Chart($("chart"), {
    type: "line",
    data: { labels, datasets: [{ data, stepped: "before", borderColor: "#e8ecff", borderWidth: 2, pointRadius: 5,
      pointBackgroundColor: points, pointBorderWidth: 0 }] },
    options: {
      responsive: true, maintainAspectRatio: false, animation: false,
      plugins: { legend: { display: false }, tooltip: { callbacks: {
        label: (ctx) => {
          const t = current && current.trades[ctx.dataIndex - 1];
          return t ? `${t.trader}: ${t.side} ${t.shares.toFixed(1)} sh → p=${fmtPrice(t.price_after)}` : `open p=${fmtPrice(ctx.parsed.y)}`;
        },
      } } },
      scales: {
        y: { min: 0, max: 1, ticks: { color: "#8d93b5", font: MONO }, grid: { color: "#2a2a3a" } },
        x: { ticks: { color: "#8d93b5", font: MONO, maxRotation: 60, autoSkip: true }, grid: { display: false } },
      },
    },
  });
  document.fonts?.ready.then(() => chart && chart.update("none"));
}

function renderTrades(m) {
  if (!m.trades.length) return render($("trades"), h("p", { class: "muted" }, "No trades yet."));
  render($("trades"), table(["#", "round", "trader", "side", "shares", "cost", "price"],
    m.trades.map((t, i) => [String(i + 1), t.escalation ? `esc ${t.escalation}` : String(t.round),
      h("span", {}, t.trader, t.kind === "human" ? h("span", { class: "badge" }, "human") : null),
      h("span", { class: t.side === "YES" ? "right" : "wrong" }, t.side), t.shares.toFixed(2), fmtUsd(t.cost),
      `${fmtPrice(t.price_before)} → ${fmtPrice(t.price_after)}`])));
}

function renderBeliefs(m) {
  if (!m.beliefs_revealed) {
    return render($("beliefs"), h("p", { class: "muted" }, "Sealed until the market closes, so no trader (or judge) can copy another."));
  }
  if (!m.beliefs || !m.beliefs.length) return render($("beliefs"), h("p", { class: "muted" }, "No beliefs recorded."));
  render($("beliefs"), m.beliefs.map((b) => h("article", { class: "belief" },
    h("header", {},
      h("strong", { class: "mono" }, b.trader),
      b.escalation ? h("span", { class: "badge" }, `escalation ${b.escalation}`) : null,
      h("span", { class: "muted small" }, b.model || ""),
      h("span", { class: "belief-p num" }, b.error ? "no answer" : `p(yes)=${fmtPrice(b.p_yes)}`)),
    b.error ? h("p", { class: "wrong small" }, b.error) : null,
    b.rationale ? h("p", { class: "prose" }, b.rationale) : null,
    b.key_risks && b.key_risks.length ? h("ul", { class: "risks" }, b.key_risks.map((r) => h("li", {}, String(r)))) : null,
    h("p", { class: "muted small num" },
      `${b.input_tokens ?? 0} in / ${b.output_tokens ?? 0} out tokens · ${fmtMs(b.latency_ms)} · ${fmtUsd(b.cost_usd)}`))));
}

function renderCards(m) {
  const gateTone = m.gate.state === "success" ? "right" : m.gate.state === "failure" ? "wrong" : "muted";
  render($("gate"), m.gate.state
    ? [h("p", { class: `gate-big mono ${gateTone}` }, m.gate.state), h("p", { class: "mono" }, m.gate.description || "")]
    : h("p", { class: "muted" }, "Not gated yet: auto-merge at a high closing price, blocked below the review line, escalation rounds in between."));
  const s = m.settlement;
  const counts = s.holdouts_total ? `${s.holdouts_passed} / ${s.holdouts_total} passed` : null;
  render($("settlement"),
    dl([["resolution", m.resolution], ["outcome", m.outcome], ["checks", counts], ["settled", m.settled_at ? fmtTime(m.settled_at) : null]]),
    s.findings.length
      ? h("ul", { class: "findings" }, s.findings.map((f) => h("li", {}, h("span", { class: `badge sev-${f.severity}` }, f.severity), " ", f.title)))
      : null,
    m.resolution ? null : h("p", { class: "muted small" }, "Settles on what happens after merge: a revert or bug fix within the window → NO, a quiet window → YES. Rejected PRs are refunded."));
  render($("cost"), dl([["LLM cost", fmtUsd(m.cost_usd)], ["trader latency", fmtMs(m.latency_ms)],
    ["liquidity b", String(m.b)], ["opened", fmtTime(m.created_at)], ["closed", m.closed_at ? fmtTime(m.closed_at) : null],
    ["head", m.head_sha ? m.head_sha.slice(0, 10) : null]]));
  render($("timeline"), m.events.map((e) => h("li", {},
    h("span", { class: "mono" }, e.from_state ? `${e.from_state} → ` : "", h("strong", {}, e.to_state)),
    h("span", { class: "muted small" }, ` ${e.actor} · ${fmtTime(e.created_at)}`),
    e.detail ? h("div", { class: "small" }, e.detail) : null)));
}

function renderDiff(text) {
  const { lines, add, del } = parseDiff(text);
  $("diff-stats").textContent = lines.length ? `+${add} −${del}` : "";
  if (!lines.length) return render($("diff"), h("p", { class: "muted" }, "No diff stored."));
  const cls = diffLineClass;
  const shown = lines.slice(0, DIFF_LINE_LIMIT);
  render($("diff"), h("pre", {}, shown.map((l) => h("span", { class: `dl ${cls(l)}` }, `${l}\n`))),
    lines.length > shown.length ? h("p", { class: "muted small" }, `${lines.length - shown.length} more lines not shown.`) : null);
}

async function refresh() {
  if (finished) return;
  let m;
  try {
    m = await getJSON(`/api/markets/${id}`);
  } catch (err) {
    if (err.status === 404) {
      finished = true;
      render($("hero"), h("div", { class: "error-banner" }, `Market ${id} not found. `, h("a", { href: "/" }, "Back to the andon.")));
      return;
    }
    throw err;
  }
  current = m;
  renderHero(m);
  renderChart(m);
  renderTrades(m);
  renderBeliefs(m);
  renderCards(m);
  if (m.diff_text !== lastDiff) renderDiff((lastDiff = m.diff_text));
  if (m.description_text !== lastDescription) {
    lastDescription = m.description_text;
    $("description").textContent = m.description_text || "(no description)";
  }
  finished = m.state === "SETTLED" || (m.state === "VOID" && m.resolution !== null);
}

if (Number.isInteger(id) && id > 0) poll(refresh);
else render($("hero"), h("div", { class: "error-banner" }, "Bad market id. ", h("a", { href: "/" }, "Back to the andon.")));
