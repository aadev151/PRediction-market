# PLAN — PRediction Market × Crossroads (Indy Init 2026)

## One-liner
A software factory where every PR gets its own **prediction market**: AI trader agents bet on
"will this PR pass the hidden holdout scenarios and survive the red team?" The price decides what
merges. The factory ships **Crossroads**, a blocked-train-crossing app for Indiana.

Pitch: "StrongDM measures whether agent code works as a probability. We let agents trade on that
probability, and the price decides what ships. Humans only review what the market doesn't trust."

## Hard constraints (event rules)
- Public GitHub repo, blank at 9:30. Devpost submission closes **5:00 PM sharp**. Submit by 4:40.
- Judging is at our table, science-fair style. We show the product AND the machinery, and defend decisions.
- **Holdout scenarios and league ground truth NEVER go in the public repo.** They live in a private
  repo (`*-holdouts`) and are fetched at settlement time with `HOLDOUTS_TOKEN`.

## Stack (one language for speed)
- Python 3.11+, FastAPI, Pydantic, SQLAlchemy (SQLite for dev and tests, Postgres in prod if needed), pytest, ruff.
- Frontend: static `index.html` + Leaflet + vanilla JS, served by FastAPI.
- Two deployables from the same repo: `app` (Crossroads) and `factory` (market server + dashboard).
  Use whatever host we already have accounts on (Railway/Render/Fly).
- LLM: Anthropic API. Traders use a cheap, fast model. The red-team agent uses a DIFFERENT model than the security trader.

## Repo layout
```
app/                    Crossroads (FastAPI)
  main.py  db.py  models.py  risk.py
  routes/ crossings.py  reports.py  admin.py
  static/ index.html  map.js
  data/   indiana_crossings.json  blocked_reports.json   (from scripts/ingest.py)
factory/
  market/lmsr.py        price, cost (logsumexp), trade_to, budget-capped trade
  market/engine.py      open -> trading rounds -> close -> gate -> settle -> payouts/bankrolls
  traders/              diff_reader.py coverage.py security.py skeptic.py  + prompts/*.md
  settle/runner.py      unit tests + holdouts + red team -> outcome (YES/NO)
  redteam/scripted.py   deterministic attacks: auth bypass, injection, validation, rate limit
  redteam/agent.py      LLM red team, fixed attack budget, different model
  league/mutate.py      synthetic PRs: ~40% bug-injected, ~60% benign
  league/run.py         run N markets offline, Brier vs baselines, calibration data
  server.py             API, GitHub commit statuses, dashboard, judge betting
  dashboard/            markets list, market page, calibration plot, Brier table, bankroll chart
scripts/ingest.py       pull and clean FRA Indiana data -> app/data/
tests/                  app/ and factory/ (LMSR property tests are mandatory)
.github/workflows/      ci.yml  market.yml  settle.yml
.github/pull_request_template.md
docs/DECISIONS.md       decision log (judges WILL ask)
CLAUDE.md  README.md  Makefile
```

## Component specs

### 1. LMSR market (`factory/market/lmsr.py`)
- Binary market. `price = 1/(1+exp(-(q_yes-q_no)/b))`, `cost = b*logsumexp(q_yes/b, q_no/b)`.
- `trade_to(q, b, p_target)` moves the price to the target. `budget_trade(q, b, belief, max_spend)`
  binary-searches the target between the current price and the belief so that cost <= max_spend.
- Defaults: `b = 20`, starting bankroll 100 per trader, max spend per market = 25% of current bankroll.
- Tests: price in (0,1); cost is monotonic; buying YES raises the price; round-trip trade nets ~0;
  market-maker loss <= b*ln2.

### 2. Traders (`factory/traders/`)
Each trader sees DIFFERENT information and returns JSON `{p_yes: float, rationale: str}` (clamp to [0.02, 0.98]).
- `diff_reader`: diff only.
- `coverage`: diff + test diff + coverage delta.
- `security`: diff + route/auth map + semgrep findings.
- `skeptic`: diff + PR description, prompted to find reasons it fails.
- **One LLM call per trader per market** for the belief. Then 3 mechanical trading rounds in random
  order. Traders see the price only, never each other's rationale.
- Traders NEVER see PR labels, branch names, or titles that could reveal synthetic or bug status.

### 3. Market engine and gate (`factory/market/engine.py`)
- Question: "Will this PR pass all holdout scenarios AND the red team?" A YES share pays 1.
- Gate on the final price, posted as a GitHub commit status with context `pr-market`, a description, and a market page link:
  - `p >= 0.90` -> success, "auto-merge"
  - `0.60 <= p < 0.90` -> pending, triggers an extra red-team pass
  - `p < 0.60` -> failure, "human review required"
- **Settle EVERY market, including auto-merged ones** (otherwise the calibration is biased).
- Payout: shares × outcome. Bankrolls persist across markets in SQLite. Log every trade.

### 4. Settlement (`factory/settle/runner.py`, `settle.yml`)
- Outcome YES iff all holdout scenarios pass AND red team finds no confirmed vulnerability.
- Holdouts: pytest-style HTTP scenarios run with FastAPI TestClient (fast, in-process).
- Fetch holdouts from the private repo at runtime. **Logs print only pass/fail counts, never scenario content.**
- Post the outcome to `POST /markets/{id}/settle` signed with HMAC (`SETTLE_HMAC_SECRET`).

### 5. Red team (`factory/redteam/`)
- `scripted.py` (deterministic, always runs): call admin routes without a token, oversized or malformed
  payloads, SQL/HTML injection strings, report spam to test the rate limit.
- `agent.py`: LLM attacker with a fixed budget (e.g. 15 requests). It only counts a vuln if a scripted
  reproducer confirms it.

### 6. Synthetic league (`factory/league/`)
- `mutate.py` generates ~120 diffs against `app/`. About 40% inject bugs: delete an auth dependency, flip a comparison,
  remove validation, off-by-one in the time window, drop the rate limit, wrong status code. About 60% are benign:
  refactors, renames, docstrings, AND benign deletions, so "has a deleted line" doesn't give it away.
- Mutation metadata goes to the PRIVATE repo only.
- `run.py`: for each diff, apply it in a temp worktree, run the market, settle in-process, and record results. Concurrency 8.
- Output: Brier score for the **market vs. simple average vs. majority vote vs. best single trader**, a
  calibration table (0.1 bins with n per bin), and bankroll trajectories. Always display n.

### 7. Factory server and dashboard (`factory/server.py`)
- GitHub webhook / `market.yml` -> open market -> traders -> post status.
- Pages: live markets, a market page (price chart, trades, trader rationales after close), a calibration
  plot, the Brier comparison table, bankroll leaderboard, and an andon board (red/yellow/green per open PR).
- **Judge betting**: the judge gets a 100-credit wallet and trades on the live demo market from their phone (QR code).

### 8. Crossroads app (`app/`)
- Data: FRA blocked crossing reports (Indiana) + FRA crossing inventory (lat/lon), via `scripts/ingest.py`.
  If there's no bulk export, scrape a reasonable Indiana sample and label it as a sample in the UI.
- Endpoints:
  - `GET /api/crossings?bbox=` crossings with a risk score
  - `GET /api/crossings/{id}` history and hour-of-week block risk
  - `GET /api/hotlist` "3-in-30" tracker: crossings 1–2 incidents away from FRA's investigation threshold
  - `POST /api/reports` crowd report (validated, rate-limited)
  - `GET /api/admin/reports`, `POST /api/admin/reports/{id}/approve|delete` **require `ADMIN_TOKEN`**
    (this is the auth check we delete in the live demo)
- `risk.py`: Gamma-Poisson empirical Bayes. Shrink per-crossing hour-of-week rates toward the county mean.
- UI: Leaflet map colored by risk, hotlist sidebar, "Report blocked crossing" button that produces a
  **draft FRA report text to copy. NEVER submit anything to the real FRA portal.** Add an "Unofficial" footer.

## CI (`ci.yml`, required checks: `ci` + `pr-market`)
ruff, pytest (app + factory), semgrep (python + OWASP rules), gitleaks, pip-audit.

## What CLAUDE.md must contain
1. A 3-line project summary + pointer to this PLAN.md.
2. **Hard rules:**
   - Never look for, read, or request holdouts, league ground truth, or `HOLDOUTS_TOKEN`.
   - Never weaken, skip, or delete tests, CI, semgrep rules, or the `pr-market` check to get green.
   - Never print or commit secrets. Use `.env.example` only.
   - Never submit anything to the real FRA portal.
   - Small PRs: one concern, < 300 changed lines, with tests.
   - Every route declares its auth requirement explicitly. Admin routes require `ADMIN_TOKEN`.
3. Conventions: type hints, Pydantic models at API boundaries, ruff format, tests mirror the source tree.
4. Commands: `make dev | test | lint | league N=120 | demo-bug | dashboard`.
5. For any non-trivial choice, append to `docs/DECISIONS.md`: decision, alternatives rejected, why.
6. The PR template has sections: What / Why / Tests / Risk.

## Env / secrets (`.env.example`)
`ANTHROPIC_API_KEY  GITHUB_TOKEN  HOLDOUTS_TOKEN  SETTLE_HMAC_SECRET  ADMIN_TOKEN  DATABASE_URL  FACTORY_URL`

## Holdouts (private repo, written in a SEPARATE agent session)
- Write them from the API contract in section 8 only. That session never opens the app code.
- ~15 scenarios: happy paths, auth (admin without a token -> 401/403), validation, hotlist correctness on a
  fixed fixture, rate limit, risk ordering sanity.

## Timeline (finish by 4:40)
| By | Track A: market + traders + dashboard | Track B: app + holdouts + red team + CI |
|---|---|---|
| +45 min | CLAUDE.md, Makefile, LMSR + tests green | repo scaffold, ci.yml, ingest script, app skeleton |
| 1:00 | engine + 4 traders on a sample diff | app MVP: map + hotlist + reports + admin; deployed |
| 2:30 | server: webhook -> status check end to end | holdouts (private repo), settlement, scripted red team |
| 3:30 | league run (N≈120), Brier table + calibration plot | LLM red team, `make demo-bug`, judge wallet |
| 4:15 | andon board, polish | README, DECISIONS.md, Devpost text |
| 4:40 | **SUBMIT** | **SUBMIT** |

## Demo script (90 sec)
1. Hook: "How do you prove agent code works when agents wrote the tests too? We price it."
2. Show the Crossroads map and the 3-in-30 hotlist (real Indiana problem; the state can't fine railroads).
3. `make demo-bug`: a PR that deletes the admin auth check. The security trader dumps it, the price crashes,
   the PR is blocked, and the red team confirms the hole. Same diff every time, rehearsed.
4. Hand the judge the QR code: 100 credits, bet against our agents on a live PR (settles in < 2 min).
5. Show the Brier table: market vs. average vs. majority vs. best single trader, n labeled.
6. "Why not just average?" Traders hold different information, and bankrolls update multiplicatively
   with accuracy (Hedge-like), so the market learns whom to trust. A vote doesn't.