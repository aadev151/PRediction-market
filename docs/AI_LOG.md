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
