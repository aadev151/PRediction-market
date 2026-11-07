# PRediction Market

AI agents trade on whether each pull request is correct. The price decides what merges.

Every PR in a connected repo opens a prediction market. Four Claude-powered traders, each with different evidence (the diff, the tests,
risk signals, an adversarial read), set a price through an LMSR market maker. The price becomes the `pr-market` GitHub status check:
auto-merge ≥ 0.90, blocked < 0.60, and in between the traders see each other's reasoning and trade again (escalation rounds; still
undecided → blocked, "no consensus"). Merged PRs settle on what happens after merge (a revert or bug fix within 14 days), so trader
bankrolls and calibration are earned; rejected PRs are refunded. For the hackathon, hidden holdout tests run in a sandbox stand in
for that post-merge outcome.

> Work in progress (Indy Init 2026). See `REQUIREMENTS.md` (why) and `PLAN.md` (how). Contributor and agent rules: `CLAUDE.md`.

## Quick start

```bash
make setup                 # Python 3.12 venv via uv + dev deps
make check                 # ruff + pytest (fake LLM, no network)
.venv/bin/prm validate     # validate prm.server.toml
```

`examples/sample-target/` (coming in WP C) is a **test fixture**, not the product: a tiny service the factory gates in dev and CI.
