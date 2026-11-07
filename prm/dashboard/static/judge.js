// WP J. Judge page `/judge` (PLAN §6.14, R-UI-2), for a phone: join with a name -> 100 credits -> the same
// diff the agents saw + the live price -> YES/NO with a capped spend slider -> position -> P&L after settle.
// Untrusted text (diff, description, names) only reaches the DOM as text nodes.
import { diffLineClass, fmtPrice, getJSON, h, parseDiff, poll, render } from "./prm.js";
import { errorText, fmtCredits, fmtSigned, payoffs, phase, positionFor, postJSON, remaining } from "./judge-core.js";

const $ = (x) => document.getElementById(x);
let me = null; // /api/judges/me, null until joined
let market = null; // the current judge market summary
let shownDiffFor = null;
let side = null;
let busy = false;

// ------------------------------------------------------------------------------------------------ join

$("join-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const name = $("name").value.trim();
  if (!name) return;
  $("join-msg").textContent = "joining…";
  const r = await postJSON("/api/judges", { name });
  if (!r.ok) {
    $("join-msg").textContent = errorText(r);
    return;
  }
  $("join-msg").textContent = "";
  await refresh();
});

// ------------------------------------------------------------------------------------------------- bet

for (const btn of document.querySelectorAll(".side")) {
  btn.addEventListener("click", () => {
    side = btn.dataset.side;
    for (const b of document.querySelectorAll(".side")) b.setAttribute("aria-pressed", String(b === btn));
    syncBet();
  });
}
$("spend").addEventListener("input", syncBet);

$("place").addEventListener("click", async () => {
  if (!market || !side || busy) return;
  busy = true;
  syncBet();
  const spend = Number($("spend").value);
  const r = await postJSON(`/api/markets/${Number(market.id)}/human-trades`, { side, spend });
  busy = false;
  $("bet-msg").textContent = r.ok
    ? `Bought ${r.data.shares.toFixed(2)} ${r.data.side} shares for ${fmtCredits(r.data.cost)}. Price ${fmtPrice(r.data.price_before)} → ${fmtPrice(r.data.price_after)}.`
    : errorText(r);
  $("bet-msg").className = `msg ${r.ok ? "right" : "wrong"}`;
  await refresh();
});

function syncBet() {
  const room = market ? remaining(me, market.id) : 0;
  const slider = $("spend");
  slider.max = String(Math.max(1, Math.floor(room)));
  slider.disabled = room < 1;
  if (Number(slider.value) > room) slider.value = String(Math.max(1, Math.floor(room)));
  $("spend-out").textContent = slider.value;
  $("cap-note").textContent = room < 1 ? "· you've used your cap for this market" : `· up to ${fmtCredits(room)}`;
  const btn = $("place");
  btn.disabled = busy || !side || room < 1;
  btn.textContent = busy ? "placing…" : side ? `Bet ${slider.value} on ${side}` : "Pick a side";
}

// ---------------------------------------------------------------------------------------------- render

function renderMe() {
  $("join").hidden = me !== null;
  $("me").hidden = me === null;
  if (!me) return;
  render($("me"),
    h("div", {}, h("div", { class: "muted small" }, "judge"), h("strong", { class: "mono" }, me.name)),
    h("div", {}, h("div", { class: "muted small" }, "credits"), h("strong", { class: "num" }, fmtCredits(me.bankroll))),
    h("div", {}, h("div", { class: "muted small" }, "P&L"),
      h("strong", { class: `num ${me.pnl > 0 ? "right" : me.pnl < 0 ? "wrong" : ""}` }, fmtSigned(me.pnl))));
}

function renderMarket() {
  const ph = phase(market);
  const box = $("market");
  if (ph === "none") {
    render(box, h("div", { class: "empty" }, "Waiting for the presenter to open a judge market…"));
    return;
  }
  const label = {
    agents: "the agents are trading…",
    betting: "your turn: place a bet",
    settling: "betting closed · running the hidden tests…",
    settled: `settled ${market.outcome}`,
    void: "market void · everyone refunded",
  }[ph];
  render(box, h("div", { class: `card judge-hero phase-${ph}` },
    h("div", { class: "muted small" }, `judge market ${market.id} · ${ph === "settled" ? "closing price" : "price = P(it holds up)"}`),
    h("div", { class: "price" }, fmtPrice(market.price)),
    h("div", { class: "mono" }, label)));
}

function renderPosition() {
  const pos = market ? positionFor(me, market.id) : null;
  $("position").hidden = !pos;
  if (!pos) return;
  const pay = payoffs(pos);
  const kids = [h("h2", {}, "Your position"),
    h("dl", { class: "kv" },
      h("dt", {}, "YES shares"), h("dd", {}, `${pay.yes.toFixed(2)} (pays ${fmtCredits(pay.yes)} if YES)`),
      h("dt", {}, "NO shares"), h("dd", {}, `${pay.no.toFixed(2)} (pays ${fmtCredits(pay.no)} if NO)`),
      h("dt", {}, "spent"), h("dd", {}, `${fmtCredits(pos.spent)} of ${fmtCredits(pos.cap)}`))];
  if (pos.payout !== null && pos.payout !== undefined) {
    const won = pos.pnl > 0;
    kids.push(h("p", { class: `result ${won ? "right" : pos.pnl < 0 ? "wrong" : "muted"}` },
      market.state === "VOID" ? `refunded ${fmtCredits(pos.payout)}` : `${won ? "You beat the market" : "Paid"} ${fmtCredits(pos.payout)} · P&L ${fmtSigned(pos.pnl)}`));
  }
  render($("position"), kids);
}

async function renderDiff() {
  if (!market || shownDiffFor === market.id) return;
  const d = await getJSON(`/api/markets/${Number(market.id)}`);
  shownDiffFor = market.id;
  $("description").textContent = d.description_text || "";
  const { lines } = parseDiff(d.diff_text);
  render($("diff"), lines.map((l) => h("span", { class: `dl ${diffLineClass(l)}` }, l || " ")));
  $("diff-card").hidden = false;
}

async function loadMe() {
  try {
    me = await getJSON("/api/judges/me");
  } catch (err) {
    if (err.status !== 401) throw err;
    me = null;
  }
}

async function refresh() {
  const [cur] = await Promise.all([getJSON("/api/demo/current"), loadMe()]);
  if (market?.id !== cur.market?.id) {
    side = null;
    $("bet-msg").textContent = "";
    for (const b of document.querySelectorAll(".side")) b.setAttribute("aria-pressed", "false");
  }
  market = cur.market;
  renderMe();
  renderMarket();
  $("bet").hidden = !(me && phase(market) === "betting");
  syncBet();
  renderPosition();
  await renderDiff();
}

poll(refresh, 2000, "live");
