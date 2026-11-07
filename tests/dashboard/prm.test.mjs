// WP F. Unit tests for prm/dashboard/static/prm.js, run by test_static.py with `node --test`.
import assert from "node:assert/strict";
import { test } from "node:test";

import {
  calibrationPoints, diffLineClass, fmtCi, fmtMs, fmtNum, fmtPct, fmtPrice, fmtUsd, githubUrl, marketLabel,
  marketStatus, methodRows, parseDiff, safeHref,
} from "../../prm/dashboard/static/prm.js";

test("methodRows keeps a fixed order, skips missing methods and surfaces the honesty flags", () => {
  const s = (brier, extra = {}) => ({ n: 40, brier, brier_ci: [0.12, 0.22], log_loss: 0.8, log_loss_ci: [0.6, 1], ...extra });
  const rows = methodRows({
    base_rate: s(0.24, { flag: "in-sample", rate: 0.6 }),
    market: s(0.17),
    best_single: s(0.2, { flag: "chosen in hindsight", trader: "coverage" }),
  });
  assert.deepEqual(rows.map((r) => r.key), ["market", "best_single", "base_rate"]);
  assert.equal(rows[0].note, "");
  assert.equal(rows[1].note, "coverage · chosen in hindsight");
  assert.equal(rows[2].note, "rate 0.60 · in-sample");
  assert.deepEqual([rows[0].brier, rows[0].brierCi, rows[0].n], [0.17, [0.12, 0.22], 40]);
  assert.deepEqual(methodRows(undefined), []);
});

test("calibrationPoints drops empty bins and sizes bubbles by n", () => {
  const pts = calibrationPoints([
    { bin: [0, 0.1], n: 0, mean_p: null, observed: null },
    { bin: [0.4, 0.5], n: 4, mean_p: 0.46, observed: 0.25 },
    { bin: [0.9, 1], n: 16, mean_p: 0.94, observed: 1 },
  ]);
  assert.deepEqual(pts.map((p) => [p.x, p.y, p.n]), [[0.46, 0.25, 4], [0.94, 1, 16]]);
  assert.ok(pts[1].r > pts[0].r);
  assert.equal(pts[1].r, 18);
  assert.deepEqual(calibrationPoints(null), []);
});

test("report formatters", () => {
  assert.equal(fmtPct(33.333), "33.3%");
  assert.equal(fmtPct(null), "—");
  assert.equal(fmtNum(0.27401), "0.274");
  assert.equal(fmtCi([0.1875, 0.3766]), "[0.188, 0.377]");
  assert.equal(fmtCi(null), "—");
});

const base = { id: 1, repo: "acme/widgets", pr_number: 7, source: "github", escalations: 0, gate_description: null };
const m = (over) => ({ ...base, ...over });

test("open markets are grey and say what they are doing", () => {
  assert.deepEqual(marketStatus(m({ state: "TRADING" })), { tone: "open", label: "trading", verdict: null });
  assert.equal(marketStatus(m({ state: "TRADING", escalations: 2 })).label, "escalation 2");
  assert.equal(marketStatus(m({ state: "CREATED" })).label, "opening");
  assert.equal(marketStatus(m({ state: "HUMAN_WINDOW" })).label, "humans trading");
  assert.equal(marketStatus(m({ state: "CLOSED" })).label, "gating");
});

test("gate colors: green auto-merge, red blocked, yellow no consensus", () => {
  assert.equal(marketStatus(m({ state: "GATED", gate_state: "success" })).tone, "green");
  assert.equal(marketStatus(m({ state: "GATED", gate_state: "failure", gate_description: "blocked (p=0.2)" })).tone, "red");
  const nc = marketStatus(m({ state: "GATED", gate_state: "failure", gate_description: "no consensus (p=0.74)" }));
  assert.deepEqual([nc.tone, nc.label], ["yellow", "no consensus"]);
});

test("an error VOID is grey; a refunded (rejected) VOID keeps its gate color", () => {
  const err = m({ state: "VOID", resolution: "void", gate_state: "failure" });
  assert.deepEqual([marketStatus(err).tone, marketStatus(err).label], ["void", "void · human review"]);
  const refunded = m({ state: "VOID", resolution: "refunded", gate_state: "failure", gate_description: "blocked (p=0.3)", outcome: "NO" });
  assert.equal(marketStatus(refunded).tone, "red");
  assert.deepEqual(marketStatus(refunded).verdict, { right: true, text: "refunded · check ✓ agrees" });
  assert.equal(marketStatus({ ...refunded, outcome: "YES" }).verdict.right, false);
});

test("a paid market is right when (gate succeeded) == (outcome YES), like status_description", () => {
  const paid = (gate, outcome) => marketStatus(m({ state: "SETTLED", resolution: "paid", gate_state: gate, outcome })).verdict;
  assert.deepEqual(paid("success", "YES"), { right: true, text: "settled YES · ✓ market right" });
  assert.equal(paid("success", "NO").right, false);
  assert.equal(paid("failure", "NO").right, true);
  assert.equal(paid("failure", "YES").right, false);
  assert.equal(marketStatus(m({ state: "GATED", gate_state: "success" })).verdict.text, "awaiting post-merge outcome");
});

test("safeHref allows same-origin paths and https only", () => {
  assert.equal(safeHref("/m/3"), "/m/3");
  assert.equal(safeHref("https://github.com/a/b/pull/1"), "https://github.com/a/b/pull/1");
  for (const bad of ["javascript:alert(1)", "//evil.example", "data:text/html,x", "http://x", "https://a b"]) {
    assert.equal(safeHref(bad), "#", bad);
  }
});

test("labels and GitHub links", () => {
  assert.equal(marketLabel(base), "acme/widgets#7");
  assert.equal(marketLabel(m({ pr_number: null, source: "demo", id: 4 })), "acme/widgets · demo 4");
  assert.equal(githubUrl(base), "https://github.com/acme/widgets/pull/7");
  assert.equal(githubUrl(m({ source: "demo" })), null);
  assert.equal(githubUrl(m({ repo: "x/y\" onclick" })), null);
});

test("formatters", () => {
  assert.equal(fmtPrice(0.9458), "0.95");
  assert.equal(fmtPrice(null), "—");
  assert.equal(fmtUsd(0), "$0");
  assert.equal(fmtUsd(0.0012), "$0.0012");
  assert.equal(fmtUsd(1.5), "$1.50");
  assert.equal(fmtMs(850), "850 ms");
  assert.equal(fmtMs(2340), "2.3 s");
  assert.equal(fmtMs(0), "—");
});

test("diff parsing counts +/- lines but not file headers", () => {
  const diff = "diff --git a/x b/x\n--- a/x\n+++ b/x\n@@ -1,2 +1,2 @@\n ctx\n-old\n+new\n+more\n";
  const { lines, add, del } = parseDiff(diff);
  assert.deepEqual([lines.length, add, del], [8, 2, 1]);
  assert.deepEqual(lines.map(diffLineClass), ["file", "meta", "meta", "hunk", "", "del", "add", "add"]);
  assert.deepEqual(parseDiff(""), { lines: [], add: 0, del: 0 });
  assert.deepEqual(parseDiff(null).lines, []);
});
