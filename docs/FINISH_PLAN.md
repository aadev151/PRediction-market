# Finish plan: from "works offline" to "works in the demo"

Written 2026-11-07 16:25 EST, after WP J merged. Supersedes the remaining rows of PLAN §9/§10 for the last stretch.
**PLAN §10 has the final Devpost submit at 4:40.** If that still holds, do step 0 first and treat the rest as best effort.

## Where we are (checked on `main`)

| Works | Not done |
|---|---|
| `make check`: 927 passed | No webhook on any GitHub repo: the live `pr-market` path has never run |
| `make demo-bug-local`: blocked p=0.43, settled NO, market right | Real Claude traders never run end to end (all runs so far used the fake LLM) |
| `make demo-good-local`: auto-merge p=0.91, settled YES, market right | `docs/LEAGUE.md` numbers come from **fake** beliefs (labeled as such) |
| Dashboard: andon, market, league, how, judge, live | README is a stub; `docs/INTEGRATING.md` doesn't exist |
| Judge betting end to end (QR → bet → settle → leaderboard) | ~~Real PRs stop at GATED~~ merge webhook + "Fast-forward 14 days" button (#73) |

## Steps (in order, each time-boxed)

### 0. Devpost draft (humans, 10 min). Do this now if 4:40 holds
Submit a draft with the offline-demo screenshots (andon, the blocked market page, `/league`, `/live`). Edit it later.

### 1. Real-model check (5 min). Unblocks 2 and 4
```
make smoke-llm                      # needs ANTHROPIC_API_KEY in your shell; expect one Belief per model
PRM_DB=real.db .venv/bin/prm demo bug --local --target examples/sample-target --repo local/sample-target
```
Done when: the second command prints `blocked … settled NO` with real model names on the market page. If the
API fails, demo on the fake LLM and say so ("deterministic offline traders"). That's a valid fail-closed story.

### 2. Live GitHub PR with a red `pr-market` (25 min, WP K). The 60 s pitch depends on it
1. Create a **public** test repo (e.g. `aadev151/prm-demo-target`) from `examples/sample-target/`, **without** the
   hidden tests. **Remove the `holdouts_path` line from its `.prm.toml`:** a remote config with that key is rejected
   (`parse_target_config`), so the PR would get "invalid .prm.toml on base branch". Push it to `main`.
2. Start the server with real traders and a public URL (not `make dev`, which forces the fake LLM):
   ```
   make tunnel                         # terminal 1: note the https://….trycloudflare.com URL
   export PRM_PUBLIC_URL=<that URL> PRM_FAKE_LLM=0 PRM_DEMO_TARGET=examples/sample-target
   # also in this shell: ANTHROPIC_API_KEY, GITHUB_TOKEN (repo:status on the test repo),
   # GITHUB_WEBHOOK_SECRET (any random string), PRM_ADMIN_TOKEN (any random string)
   .venv/bin/prm repo add aadev151/prm-demo-target
   .venv/bin/prm serve                 # terminal 2
   ```
3. Test repo → Settings → Webhooks: payload URL `<PRM_PUBLIC_URL>/api/hooks/github`, content type
   `application/json`, the same secret, events: **Pull requests** only. Check that the ping gets a 200.
4. `git checkout -b demo-bug && git apply bug_auth.patch && git push`, then `gh pr create`. Also open one from
   `good_docstring.patch`.

Done when: the bug PR shows a red `pr-market` linking to `/m/<id>` (rationales after close, gate, timeline), and the
good PR goes green. **Leave both PRs open for the demo.** Fallback: `make demo-bug-local` on the dashboard.

### 3. README + INTEGRATING (20 min, WP H). Can run in parallel with 2
README: the one-line pitch, "Plug it into your repo in 3 steps" (`.prm.toml` → webhook → branch protection on
`pr-market`), the architecture diagram, the live link, league numbers **with n**, "How AI built this" (point at
`docs/AI_LOG.md`, the guard hook and CI), and the quick-start commands. `docs/INTEGRATING.md`: the long version of
the 3 steps, plus `.prm.toml` fields and failure statuses.

### 4. Real-model league numbers (15 min, ~$ on Haiku, WP G). Only if 1 passed
`PRM_FAKE_LLM=0 make league-sample` rewrites `docs/LEAGUE.md` and the `/league` data. Commit the report via a PR.
Done when: LEAGUE.md says `Beliefs: real`. If time is short, keep the fake numbers. They're labeled honestly.

### 5. Rehearse (humans, 15 min)
Every teammate does the 60 s pitch (PLAN §11) and can explain LMSR + fail-closed. Run `prm demo reset` before going live.

## Demo run of show (what's open on screen)
1. The GitHub PR list on the test repo: the red bug PR, the green good PR.
2. `/m/<bug market>`: price chart (the security trader sells), gate "blocked", rationales.
3. **Settlement (DECISIONS #73):** merge the good PR on GitHub, open its market page, press **Fast-forward 14
   days** → "settled YES · market right", and the PR's `pr-market` status updates. For a NO: merge a PR, push a
   `git revert` (or a `fix:` commit touching its lines) to `main`, then fast-forward → "settled NO". The bug PR
   stays blocked (unmerged), which is the point.
4. `/league` → `/how` → `docs/AI_LOG.md`.
5. `/live` on the projector + the QR → judges bet → Close & settle → leaderboard.

## Cut list (don't start these)
A background settlement poller (the button covers the demo) · remote settlement (P1) · `prm demo bug` without `--local` ·
WP L retire/respawn (P2) · the judge demo inside the Docker image (the demo runs from a laptop over the tunnel).
