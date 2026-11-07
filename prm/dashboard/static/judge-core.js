// WP J. Pure helpers for /judge and /live (R-UI-2). No DOM, no side effects, so node can unit-test them.

/** What a judge market is doing, from its state: none | agents | betting | settling | settled | void. */
export function phase(m) {
  if (!m) return "none";
  if (m.state === "CREATED" || m.state === "CONTEXT_READY" || m.state === "TRADING") return "agents";
  if (m.state === "HUMAN_WINDOW") return "betting";
  if (m.state === "CLOSED" || m.state === "GATED" || m.state === "SETTLING") return "settling";
  if (m.state === "SETTLED") return "settled";
  return "void";
}

/** The judge's position in market `marketId`, or null. */
export function positionFor(me, marketId) {
  return (me?.positions || []).find((p) => p.market_id === marketId) || null;
}

/** Credits the judge may still spend on this market: (cap - spent) or the cap for a new position, never more
 * than the bankroll. Rounded down to cents so the slider never offers more than the server allows. */
export function remaining(me, marketId) {
  if (!me) return 0;
  const pos = positionFor(me, marketId);
  const room = pos ? pos.cap - pos.spent : me.next_cap;
  return Math.max(0, Math.floor(Math.min(room, me.bankroll) * 100) / 100);
}

/** What the position pays if the market settles YES / NO. */
export function payoffs(pos) {
  return pos ? { yes: pos.yes_shares, no: pos.no_shares } : { yes: 0, no: 0 };
}

/** Leaderboard rows split into the two teams: agents of the demo repo vs every human judge, each sorted by
 * P&L (best first), with team totals. */
export function teams(traders, repo) {
  const sum = (rows) => rows.reduce((acc, t) => acc + (t.pnl || 0), 0);
  const byPnl = (a, b) => (b.pnl || 0) - (a.pnl || 0) || a.name.localeCompare(b.name);
  const agents = (traders || []).filter((t) => t.kind === "agent" && t.repo === repo).sort(byPnl);
  const judges = (traders || []).filter((t) => t.kind === "human").sort(byPnl);
  return { agents, judges, agentPnl: sum(agents), judgePnl: sum(judges) };
}

export function fmtCredits(x) {
  return typeof x === "number" && Number.isFinite(x) ? x.toFixed(2) : "—";
}

export function fmtSigned(x) {
  if (typeof x !== "number" || !Number.isFinite(x)) return "—";
  const r = Math.round(x * 100) / 100;
  return `${r > 0 ? "+" : r < 0 ? "−" : "±"}${Math.abs(r).toFixed(2)}`;
}

/** POST JSON; resolves to {ok, status, data}. The server's error text is in data.detail (string or list). */
export async function postJSON(url, body, headers = {}) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json", Accept: "application/json", ...headers },
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin",
    cache: "no-store",
  });
  let data = null;
  try {
    data = await res.json();
  } catch {
    data = null;
  }
  return { ok: res.ok, status: res.status, data };
}

/** A short, human message for an API error (FastAPI's detail is a string or a list of validation errors). */
export function errorText(r) {
  const d = r?.data?.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d) && d.length) return String(d[0].msg || "invalid input").replace(/^Value error, /, "");
  return `request failed (${r?.status ?? "network"})`;
}
