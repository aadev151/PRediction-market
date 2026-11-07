# Where the AI needed correcting

A running log of AI mistakes a human (or a check) caught. Append only. Newest at the bottom.
Format: what the AI did · how it was caught · the fix.

| # | When | What the AI got wrong | Caught by | Fix |
|---|---|---|---|---|
| 1 | 2026-11-07 ~11:45 | **Scope inherited from an old sketch.** The first REQUIREMENTS/PLAN carried PLAN_V0's "Crossroads" app forward as the gated target without checking with the team. | Human review of the plan | Re-scoped to a standalone, target-agnostic factory. |
| 2 | 2026-11-07 ~11:45 | **Fabricated regulatory threshold** in PLAN_V0 ("FRA 3-in-30 investigation threshold"). No source exists. | Human fact-check | Now moot (Crossroads dropped). Kept as an example of a confident hallucination. |
| 3 | 2026-11-07 ~11:45 | **Target-coupled design.** The first plan's security trader parsed FastAPI routes specifically. | Human review | Language-agnostic risk signals over diff lines + per-repo `.prm.toml` globs. |
| 4 | 2026-11-07 ~11:35 | **Schedule drift.** V0 assumed a 9:30 start; at 11:33 there were zero commits. | Human | Re-planned to ~5 hours with P0/P1/P2 tiers and hard checkpoints. |
| 5 | 2026-11-07 12:20 | **Ran a formatter over the specs.** During Phase 0 the agent ran `ruff format .`, which also reformatted the Python code blocks inside `PLAN.md`. | The agent's own `git diff` before committing | Reverted PLAN.md; added `*.md` to ruff's `extend-exclude` (DECISIONS #14). |
| 6 | 2026-11-07 12:57 | **Vacuous invariant test.** The first "caps respected cumulatively over rounds" test used beliefs 0.98 vs 0.02, so every trader exhausted its cap in round 1 and later rounds never ran; the test would have passed even if caps reset per round. | The agent's own extra assertion (`max(round) > 1`) failed | Beliefs 0.7 vs 0.3 so traders fight over 4 rounds; the test now also asserts the cap actually binds (spent == 25). |
| 7 | 2026-11-07 13:55 | **Concurrency and idempotency holes** (double-settle race, lost bankroll updates, non-idempotent gate, VOID market blocking re-create, unbounded gather) caught by code review, not by the agent's own tests. | Code review of `wp-a/engine` | Optimistic locking (`version_id`) + stale retries, `TradeConflict`, idempotent `gate()`, `markets.attempt`, per-belief timeout; each with a regression test (race tests on file-backed SQLite, mutation-checked to fail without the lock). |
