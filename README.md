# PRediction Market

**A betting market for code.** Every pull request (PR) opens a small market. Four AI agents bet on one question:
*"If we merge this, will it hold up?"* The final price is a number from 0 to 1, and that number decides what happens
to the PR: merge it, block it, or ask a human.

Demo repo: [aadev151/prm-demo-target](https://github.com/aadev151/prm-demo-target)
([PR #1](https://github.com/aadev151/prm-demo-target/pull/1) was blocked at 0.03,
[PR #2](https://github.com/aadev151/prm-demo-target/pull/2) was approved at 0.93).

## What it is, in one minute

AI now writes code *and* the tests for that code. So who checks the checker? Asking one AI "is this PR OK?" gives you
one opinion, and that opinion can be wrong or fooled.

We do what markets do: get **several independent opinions**, make each one **put credits behind its answer**, and
let the price combine them. Agents that are right earn credits. Agents that are wrong lose them. Over time the
reliable agents carry more weight.

The result shows up on GitHub as a normal status check called **`pr-market`**:

| Final price | What happens | Shows on GitHub as |
|---|---|---|
| **0.90 or more** | Auto-merge OK | ✅ green |
| **0.60 to 0.90** | The agents talk it over and bet again (up to 2 extra rounds). Still unsure → "no consensus" → a human decides | ❌ red |
| **under 0.60** | Blocked | ❌ red |

## How it works, step by step

1. **Someone opens a PR** in a connected GitHub repo.
2. **GitHub tells our server** (a webhook). The server reads the repo's one config file, `.prm.toml`, and downloads
   the change.
3. **Four AI traders look at it, and each sees something different:**
   - **Diff reader** (Claude Haiku): only the code change.
   - **Coverage** (Claude Haiku): the code change, the test changes, and which files were touched.
   - **Security** (Claude Sonnet): the code change, the sensitive files (auth, permissions…), and automatic risk
     warnings.
   - **Skeptic** (Claude Haiku): the code change and the PR description, read with suspicion ("does the description
     match what the code really does?").
4. **Each trader answers with one number:** how likely it thinks the PR is to hold up (for example 0.95 = "95% sure
   it's fine").
5. **They trade.** The price starts at 0.50 and moves as they buy and sell (details below).
6. **The final price becomes the green or red check on GitHub**, with a link to a page showing the price chart, every
   trade, and each trader's reasoning.
7. **Later, the market settles**: we find out who was right, and the winners get paid (see "When do bets settle?").

## How the agents bet (confidence → bet size)

- Every trader starts with **100 credits**.
- A trader **buys YES** ("it will hold up") when it thinks the price is too low, and **buys NO** when it thinks the
  price is too high.
- It keeps buying until the price matches its own confidence. **The more it disagrees with the current price, the more
  it bets.** A trader that is 95% sure, looking at a price of 0.50, bets a lot on YES. A trader that is 55% sure barely
  moves it.
- Each trader can spend **at most a quarter of its balance on one PR, and never more than 30 credits** (25 at the
  start). That caps how far one agent can push the price: even at the 30-credit maximum it reaches about **0.89** alone,
  never the 0.90 needed to merge. **No single AI can force a merge.** The server refuses to start with settings that
  would allow it, and our tests check it.
- If the price ends up in the gray zone (0.60–0.90), the traders get an **escalation round**. They read each other's
  *reasoning* (never each other's numbers), rethink, and bet again with whatever is left of their limit.
- Prices come from a standard formula for automated markets (LMSR, a *market maker*): buying YES makes YES more
  expensive, so moving the price to an extreme costs a lot.

## When do bets settle?

**After the PR is merged, based on what actually happens to it.** The rule:

- If within **14 days** of the merge someone **reverts** the PR, or pushes a **bug fix** (a commit whose message says
  `fix`, `bug`, `hotfix`, `revert` or `regression`) that changes lines this PR wrote → it settles **NO**.
- If 14 days pass quietly → it settles **YES**.
- Each YES share pays 1 credit if the answer is YES; each NO share pays 1 credit if the answer is NO. The credits go
  back into each trader's balance for the next PR.

On stage we can't wait 14 days, so the market page has a **"Fast-forward 14 days"** button. It runs the same check as
if two weeks had passed, settles the market, and updates the GitHub check to e.g. *"auto-merge OK (p=0.93) · settled
YES · market right"*.

A PR that gets **blocked is never merged**, so there's nothing to check afterwards and nobody gets paid for it. By
design those bets are returned (in this hackathon build they simply stay open on the dashboard).

Two places use **hidden tests** instead, because their changes are never merged:
- **The league** (`/league`): we generate 120 practice changes to a sample app (about 40% with a planted bug) and score
  how good the market's prices were.
- **The judge game** (`/live`): judges bet against the agents on a practice change, and hidden tests decide who won.

## Why it works

- **Different evidence, not four copies of one opinion.** Each trader sees a different slice, so they make different
  mistakes, and the market combines them.
- **Credits on the line.** Being confident costs credits, and being wrong loses them. Over time, good traders gain
  influence and bad ones lose it.
- **No single point of failure.** Spending caps mean no one agent can push a PR to merge alone.
- **The AI only gives a number.** Normal, tested code does everything else: the trading, the gate, the payouts, every
  state change. The AI can't press "merge".
- **Fails closed.** If anything goes wrong (an error, a timeout, fewer than 2 traders answering), the market is
  cancelled, everyone is refunded, and the PR is blocked for a human to review. It never merges by accident.
- **Measured, not just claimed.** The league page shows how accurate the prices are (Brier score, lower is better)
  next to simple baselines, always with the sample size *n*.

## What's on the dashboard

| Page | What it shows |
|---|---|
| `/` | The board: one tile per PR, green/red/yellow, with its price |
| `/m/<id>` | One market: price chart, every trade, each trader's reasoning, the gate, settlement, and the Fast-forward button |
| `/league` | How accurate the market is over 120 practice PRs |
| `/how` | The state machine and the safety rules, each linked to the test that checks it |
| `/live` | Presenter screen for the judge game (QR code, live price, Judges vs. Agents leaderboard) |
| `/judge` | The phone page judges get from the QR code: 100 credits, bet YES or NO |

## How to run it

**1. Try it offline (no API key, about 2 minutes).** It uses fake, deterministic traders.

```bash
make setup             # one-time: Python 3.12 environment
make check             # lint + all tests (no network needed)
make dev               # starts the server at http://localhost:8000
make demo-bug-local    # in a second terminal: a PR that removes an auth check → blocked
make demo-good-local   # a docs-only PR → auto-merge OK
```
Then open http://localhost:8000.

**2. Use real Claude traders.** Put your key in a `.env` file in the repo root (see `.env.example`); `prm` reads it
automatically:
```
ANTHROPIC_API_KEY=...
```
```bash
make smoke-llm                            # one test call per model
PRM_FAKE_LLM=0 .venv/bin/prm serve        # the server, with real traders
```

**3. Plug it into your own GitHub repo (3 steps).**
1. **Add a `.prm.toml`** to the root of your repo's main branch. Example (from the demo repo):
   ```toml
   [target]
   name = "my-repo"

   [settle]
   command = "python -m pytest -q"

   [postmerge]
   window_days = 14

   [context]
   test_globs = ["tests/**", "**/test_*.py"]
   sensitive_globs = ["**/auth*", "**/security*", "**/permissions*", ".github/**"]
   source_globs = ["**/*.py"]
   ```
2. **Run the server where GitHub can reach it.** Add `GITHUB_TOKEN`, `GITHUB_WEBHOOK_SECRET` (any random string) and
   `PRM_ADMIN_TOKEN` (any random string) to `.env`, then:
   ```bash
   cloudflared tunnel --url http://localhost:8000      # prints a public https URL
   .venv/bin/prm repo add your-name/your-repo
   PRM_FAKE_LLM=0 PRM_PUBLIC_URL=<that URL> .venv/bin/prm serve
   ```
3. **Add a webhook** in your repo: Settings → Webhooks → Add webhook. Payload URL `<that URL>/api/hooks/github`,
   content type `application/json`, the same secret, and only the **Pull requests** event.

Open a PR and the `pr-market` check appears. To make it binding, require `pr-market` in your branch protection
settings.

## How AI built this (and where it was wrong)

The whole factory was built by Claude Code agents, one per work package (see `PLAN.md` §9), under rules the agents
can't skip:
- **`CLAUDE.md`**: the project rules.
- **A guard hook** (`scripts/guard.py`) that blocks agents from reading secrets or hidden tests, or weakening tests.
- **CI**: tests, semgrep, gitleaks, pip-audit.
- **Branch protection**: nothing lands on `main` without a green PR.

Every AI mistake a human caught is logged in [`docs/AI_LOG.md`](docs/AI_LOG.md), and every design choice in
[`docs/DECISIONS.md`](docs/DECISIONS.md).

---

## Technical summary

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
