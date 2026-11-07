// WP J. Unit tests for prm/dashboard/static/judge-core.js, run by test_static.py with `node --test`.
import assert from "node:assert/strict";
import { test } from "node:test";

import { errorText, fmtSigned, payoffs, phase, positionFor, remaining, teams } from "../../prm/dashboard/static/judge-core.js";

test("phase maps every market state", () => {
  assert.equal(phase(null), "none");
  for (const s of ["CREATED", "CONTEXT_READY", "TRADING"]) assert.equal(phase({ state: s }), "agents");
  assert.equal(phase({ state: "HUMAN_WINDOW" }), "betting");
  for (const s of ["CLOSED", "GATED", "SETTLING"]) assert.equal(phase({ state: s }), "settling");
  assert.equal(phase({ state: "SETTLED" }), "settled");
  assert.equal(phase({ state: "VOID" }), "void");
});

test("remaining: the cap for a new position, cap - spent after, never above the bankroll, rounded down", () => {
  const me = { bankroll: 100, next_cap: 25, positions: [{ market_id: 3, cap: 25, spent: 10.006, yes_shares: 0, no_shares: 0 }] };
  assert.equal(remaining(me, 9), 25);
  assert.equal(remaining(me, 3), 14.99);
  assert.equal(remaining({ ...me, bankroll: 4.5 }, 9), 4.5);
  assert.equal(remaining({ ...me, positions: [{ market_id: 3, cap: 25, spent: 25 }] }, 3), 0);
  assert.equal(remaining(null, 3), 0);
});

test("positionFor and payoffs", () => {
  const me = { positions: [{ market_id: 2, yes_shares: 1.5, no_shares: 0 }] };
  assert.deepEqual(payoffs(positionFor(me, 2)), { yes: 1.5, no: 0 });
  assert.equal(positionFor(me, 7), null);
  assert.deepEqual(payoffs(null), { yes: 0, no: 0 });
});

test("teams splits the demo repo's agents from every judge and totals P&L", () => {
  const rows = [
    { name: "security", kind: "agent", repo: "demo/t", pnl: -3 },
    { name: "skeptic", kind: "agent", repo: "demo/t", pnl: 2 },
    { name: "security", kind: "agent", repo: "acme/real", pnl: 50 },
    { name: "Ada", kind: "human", repo: null, pnl: 4 },
    { name: "Bob", kind: "human", repo: null, pnl: -1 },
  ];
  const t = teams(rows, "demo/t");
  assert.deepEqual(t.agents.map((r) => r.name), ["skeptic", "security"]);
  assert.deepEqual(t.judges.map((r) => r.name), ["Ada", "Bob"]);
  assert.equal(t.agentPnl, -1);
  assert.equal(t.judgePnl, 3);
});

test("fmtSigned and errorText", () => {
  assert.equal(fmtSigned(3.456), "+3.46");
  assert.equal(fmtSigned(-2), "−2.00");
  assert.equal(fmtSigned(0.001), "±0.00");
  assert.equal(fmtSigned(undefined), "—");
  assert.equal(errorText({ status: 409, data: { detail: "betting is closed" } }), "betting is closed");
  assert.equal(errorText({ status: 422, data: { detail: [{ msg: "Value error, name must be 1-32 printable characters" }] } }),
    "name must be 1-32 printable characters");
  assert.equal(errorText({ status: 500, data: null }), "request failed (500)");
});
