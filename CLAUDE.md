# CLAUDE.md: PRediction Market

A standalone software factory that plugs into any GitHub repo. Every PR opens a prediction market: four AI traders, each seeing
**different** evidence, trade (LMSR) on "will this PR pass the repo's hidden holdout suite and red team?". The closing price is the
`pr-market` GitHub status (auto-merge ≥ 0.90, human review 0.60–0.90, blocked < 0.60). Every market is then settled by actually running the
holdouts in a sandbox, so bankrolls and calibration are earned.

- **Read `REQUIREMENTS.md` first** (why, P0/P1/P2 priorities), then **`PLAN.md`** (how: layout, contracts, work packages in §9).
- `PLAN_V0.md` is **superseded** and has been removed. Ignore its "Crossroads" app entirely. If an old note mentions it, it's stale.
- If time runs short, cut P2, then P1. Never cut the market-core tests, the fail-closed behavior, or the demo path.

## Hard rules (REQUIREMENTS §8, verbatim)

- Never look for, read, or request holdouts, league ground truth (`.league/`), or tokens (`*_TOKEN`, `*_SECRET`).
- Never weaken, skip, or delete tests, CI, semgrep rules, or the `pr-market` check to get green.
- Never print or commit secrets. Only `.env.example` is committed.
- The factory (`prm/`) contains no target-specific logic and never imports from `examples/`.
- Small PRs: one concern, under 300 changed lines, with tests.
- Every server route declares its auth explicitly.

Clarifications:
- The only holdouts you may read are the public **fixture** holdouts in `examples/sample-target-holdouts/`. Real holdouts live in private repos.
- `prm/` must not even mention `examples/` or the sample target by name; `tests/test_architecture.py` enforces it.
- Agents (LLM traders) only emit numbers. Deterministic code owns trading, gating, settlement, payouts and every state transition.
- Fail closed: any error, or fewer than 2 responding traders, VOIDs the market with gate `failure`. Never fail open into a merge.
- Holdout scenario names and content never leave the settlement sandbox. Logs, APIs and the dashboard show counts only.
- Don't invent APIs. For the Anthropic SDK use exactly the shape in PLAN §6.4 (`messages.parse` + `output_format`; no `temperature`/`top_p`/`top_k`/`thinking`).

## Conventions

- Python 3.12 via `uv` (3.11+ OK). macOS system python is 3.9: don't use it. Type hints everywhere; Pydantic at every boundary.
- **Contracts:** `prm/types.py` (shared dataclasses) and `prm/config.py` (`ServerConfig`, `TargetConfig`, `RuntimeEnv`) are shared by all work
  packages. Import from them; don't redefine. Changing them is a cross-package change and needs a line in `docs/DECISIONS.md`.
- Everything about a target comes from its `.prm.toml`, read from the **base** branch. Remote configs are parsed with
  `parse_target_config(text)` (which rejects `holdouts_path`); local ones with `load_target_config(path)`.
- Commands from `.prm.toml` are run with `render_command()` (shlex split, then placeholder fill) and `shell=False`. Never `shell=True`.
- Secrets are read from the environment at their point of use, never stored on config objects, never logged.
- Tests mirror `prm/` under `tests/` (same relative path, `test_` prefix). Tests and CI run with `PRM_FAKE_LLM=1`; no test needs network or an
  API key. LMSR property tests (hypothesis) are mandatory.
- ruff: line length 110, rules `E,F,I,B,UP,S`. Render untrusted text in the dashboard with `textContent` only.
- Each work package owns its files (PLAN §9; stub docstrings name the owner). Stay inside yours.

## Commands

```
make setup            # uv venv (3.12) + pip install -e ".[dev]"
make dev              # server with the fake LLM                      (WP E)
make test             # PRM_FAKE_LLM=1 pytest -q
make lint             # ruff check + ruff format --check
make fmt              # auto-fix
make check            # lint + test: run before every PR
make smoke-llm        # one real call per configured model            (WP B)
make settle-sample    # settle the sample target, counts only         (WP C, D)
make league-sample    # league on the sample target                   (WP G)
make demo-bug-local   # offline market on bug_auth.patch              (WP E)
make demo-good-local  # offline market on good_docstring.patch        (WP E)
make tunnel           # cloudflared fallback for a public URL
.venv/bin/prm validate [--server prm.server.toml] [--target PATH]   # validate configs
```

Before every PR: `ruff check . && ruff format --check . && pytest -q`.

## Logs we keep (append, never rewrite)

- **`docs/DECISIONS.md`**: one line per non-trivial choice (what, why).
- **`docs/AI_LOG.md`**: one line per AI mistake a human caught (wrong SDK shapes, weakened tests, hallucinated calls, scope drift…).

## PRs

Use `.github/pull_request_template.md`: **What** (one concern, which WP) / **Why** (requirement id) / **Tests** / **Risk**.
Small commits, one concern per PR, under 300 changed lines where practical, with tests.
