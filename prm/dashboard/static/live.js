// WP J. Presenter page `/live` (PLAN §6.14, R-UI-2): a big QR to /judge, the judge market's live price and
// trades, the "Judges vs. Agents" leaderboard, and the admin buttons (Open judge market, Close & settle).
// The admin token lives in sessionStorage for this tab only and is sent as a Bearer header.
import { fmtPrice, getJSON, h, marketStatus, poll, render } from "./prm.js";
import { errorText, fmtCredits, fmtSigned, phase, postJSON, teams } from "./judge-core.js";

const $ = (x) => document.getElementById(x);
const TOKEN_KEY = "prm_admin_token";
let market = null;
let detail = null;
let busy = false;

function token() {
  try {
    return sessionStorage.getItem(TOKEN_KEY) || "";
  } catch {
    return "";
  }
}

$("qr-url").textContent = `${location.origin}/judge`;
$("admin").open = !token();
$("token-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  try {
    sessionStorage.setItem(TOKEN_KEY, $("token").value.trim());
  } catch {
    /* storage blocked: the buttons will answer 401 */
  }
  $("token").value = "";
  $("admin").open = false;
  say("token saved for this tab", true);
});

function say(text, ok) {
  $("admin-msg").textContent = text;
  $("admin-msg").className = `msg ${ok ? "right" : "wrong"}`;
}

async function admin(url, pending) {
  if (busy) return;
  if (!token()) {
    $("admin").open = true;
    say("enter the presenter token first", false);
    return;
  }
  busy = true;
  syncButtons();
  say(pending, true);
  const r = await postJSON(url, undefined, { Authorization: `Bearer ${token()}` });
  busy = false;
  if (r.ok && r.data?.outcome) say(`settled ${r.data.outcome} · ${r.data.gate_description || ""}`, true);
  else say(r.ok ? "opened: the agents are trading" : errorText(r), r.ok);
  await refresh();
}

$("open-btn").addEventListener("click", () => admin("/api/demo/markets", "opening…"));
$("settle-btn").addEventListener("click", () => {
  if (market) admin(`/api/demo/markets/${Number(market.id)}/close-and-settle`, "closing · running the hidden tests…");
});

function syncButtons() {
  const ph = phase(market);
  $("open-btn").disabled = busy || !["none", "settled", "void"].includes(ph);
  $("settle-btn").disabled = busy || ph !== "betting";
}

function renderNow() {
  if (!market) {
    render($("now-body"), h("div", { class: "empty" }, "No judge market yet. Press “Open judge market”."));
    return;
  }
  const st = marketStatus(market);
  const ph = phase(market);
  const s = detail?.settlement;
  const humans = (detail?.trades || []).filter((t) => t.kind === "human").length;
  render($("now-body"),
    h("div", { class: `hero-price tone-${st.tone}` }, h("span", { class: "price" }, fmtPrice(market.price)),
      h("span", { class: "muted small" }, ph === "settled" ? "closing price" : "live price · P(it holds up)")),
    h("p", { class: "mono" },
      { agents: "agents trading…", betting: `judges betting · ${humans} judge trade(s)`, settling: "running the hidden tests…",
        settled: `settled ${market.outcome}`, void: "void · everyone refunded" }[ph]),
    ph === "settled" && s?.holdouts_total ? h("p", { class: "muted mono" }, `hidden tests: ${s.holdouts_passed}/${s.holdouts_total} passed`) : null,
    h("a", { href: `/m/${Number(market.id)}` }, `market ${market.id} details →`));
}

function renderFeed() {
  const trades = (detail?.trades || []).slice(-10).reverse();
  if (!trades.length) {
    render($("feed"), h("p", { class: "muted" }, "No trades yet."));
    return;
  }
  render($("feed"), h("table", {},
    h("thead", {}, h("tr", {}, ["who", "side", "spent", "price"].map((c) => h("th", {}, c)))),
    h("tbody", {}, trades.map((t) => h("tr", { class: t.kind === "human" ? "hl" : "" },
      h("td", {}, `${t.kind === "human" ? "🧑‍⚖️ " : "🤖 "}${t.trader}`), h("td", { class: t.side === "YES" ? "right" : "wrong" }, t.side),
      h("td", {}, fmtCredits(t.cost)), h("td", {}, `${fmtPrice(t.price_before)} → ${fmtPrice(t.price_after)}`))))));
}

function team(title, rows, total) {
  return h("div", { class: "card" },
    h("div", { class: "team-head" }, h("h3", {}, title), h("strong", { class: `num ${total > 0 ? "right" : total < 0 ? "wrong" : ""}` }, fmtSigned(total))),
    rows.length
      ? h("table", {},
        h("thead", {}, h("tr", {}, ["name", "credits", "P&L", "markets"].map((c) => h("th", {}, c)))),
        h("tbody", {}, rows.map((t) => h("tr", {},
          h("td", {}, t.name), h("td", {}, fmtCredits(t.bankroll)),
          h("td", { class: t.pnl > 0 ? "right" : t.pnl < 0 ? "wrong" : "" }, fmtSigned(t.pnl)), h("td", {}, String(t.markets_traded))))))
      : h("p", { class: "muted" }, "Nobody yet: scan the QR code."));
}

async function renderBoard() {
  if (!market) {
    render($("board"));
    return;
  }
  const board = await getJSON(`/api/leaderboard?repo=${encodeURIComponent(market.repo)}`);
  const t = teams(board.traders, market.repo);
  render($("board"), team("Judges", t.judges, t.judgePnl), team("Agents", t.agents, t.agentPnl));
}

async function refresh() {
  market = (await getJSON("/api/demo/current")).market;
  detail = market ? await getJSON(`/api/markets/${Number(market.id)}`) : null;
  renderNow();
  renderFeed();
  syncButtons();
  await renderBoard();
}

poll(refresh, 2000, "live");
