// WP F. Andon board `/` (PLAN §6.14, R-UI-1): KPI strip + one tile per market, polled every 2 s.
import { fmtClock, fmtMs, fmtPrice, fmtUsd, getJSON, h, marketLabel, marketStatus, poll, render } from "./prm.js";

const kpis = document.getElementById("kpis");
const tiles = document.getElementById("tiles");
const repoSelect = document.getElementById("repo");
const count = document.getElementById("count");

let repoFilter = new URLSearchParams(location.search).get("repo") || "";

repoSelect.addEventListener("change", () => {
  repoFilter = repoSelect.value;
  const url = new URL(location.href);
  if (repoFilter) url.searchParams.set("repo", repoFilter);
  else url.searchParams.delete("repo");
  history.replaceState(null, "", url);
  refresh();
});

function kpi(label, value, sub) {
  return h("div", { class: "card kpi" }, h("div", { class: "label" }, label), h("div", { class: "value" }, value),
    sub ? h("div", { class: "sub" }, sub) : null);
}

function renderKpis(m) {
  const brier = m.real_pr_brier == null ? "—" : m.real_pr_brier.toFixed(3);
  render(kpis,
    kpi("Markets today", String(m.markets_today ?? 0), `${m.errors ?? 0} voided by errors`),
    kpi("Real PRs priced", String(m.real_prs_priced ?? 0), "source: GitHub webhooks"),
    kpi("Real-PR Brier", brier, `n = ${m.real_pr_n ?? 0} settled (lower is better)`),
    kpi("$ / market", fmtUsd(m.usd_per_market), `${fmtUsd(m.llm_usd_today)} LLM spend today`),
    kpi("Mean latency", fmtMs(m.mean_latency_ms), "LLM wall time per market"));
}

function renderRepos(repos) {
  const names = repos.map((r) => r.full_name);
  if (repoFilter && !names.includes(repoFilter)) names.push(repoFilter);
  const current = [...repoSelect.options].slice(1).map((o) => o.value);
  if (current.join("\n") !== names.join("\n")) {
    render(repoSelect, h("option", { value: "" }, "all repos"), names.map((n) => h("option", { value: n }, n)));
  }
  repoSelect.value = repoFilter;
}

function tile(m) {
  const st = marketStatus(m);
  return h("a", { class: `tile tone-${st.tone}`, href: `/m/${Number(m.id)}`, "aria-label": `${marketLabel(m)}: ${st.label}` },
    h("div", { class: "who", title: marketLabel(m) },
      h("span", { class: "repo" }, m.repo), m.pr_number != null ? h("span", { class: "pr" }, `#${m.pr_number}`) : null),
    h("div", { class: "price" }, fmtPrice(m.price)),
    h("div", { class: "gate" }, st.label),
    h("div", { class: "meta" },
      m.source !== "github" ? h("span", { class: "badge" }, m.source) : null,
      h("span", {}, fmtClock(m.created_at)),
      m.cost_usd ? h("span", { class: "num" }, fmtUsd(m.cost_usd)) : null),
    st.verdict ? h("div", { class: `verdict ${st.verdict.right === true ? "right" : st.verdict.right === false ? "wrong" : "muted"}` },
      st.verdict.text) : null);
}

function renderTiles(markets) {
  count.textContent = `${markets.length} shown`;
  if (!markets.length) {
    render(tiles, h("div", { class: "empty" },
      repoFilter ? `No markets for ${repoFilter} yet.` : "No markets yet. Open a PR on a registered repo, or run ",
      repoFilter ? null : h("code", {}, "make demo-bug-local"), repoFilter ? null : "."));
    return;
  }
  render(tiles, markets.map(tile));
}

async function refresh() {
  const q = repoFilter ? `?repo=${encodeURIComponent(repoFilter)}&limit=200` : "?limit=200";
  const [metrics, repos, list] = await Promise.all([
    getJSON("/api/metrics"), getJSON("/api/repos"), getJSON(`/api/markets${q}`),
  ]);
  renderKpis(metrics);
  renderRepos(repos.repos || []);
  renderTiles(list.markets || []);
}

poll(refresh);
