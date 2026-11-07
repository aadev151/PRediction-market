## What
<!-- One concern. Which work package (PLAN §9)? Under 300 changed lines where practical. -->

## Why
<!-- The requirement it serves (e.g. R-MKT-3) or the problem it fixes. Non-trivial choice? Add a line to docs/DECISIONS.md. -->

## Tests
<!-- What proves it works. `ruff check . && ruff format --check . && pytest -q` is green locally. -->
- [ ] New/changed behavior has tests
- [ ] No test, CI step, semgrep rule or the `pr-market` check was weakened, skipped or deleted

## Risk
<!-- What could break, security impact, fail-closed behavior. AI mistake caught? Add a line to docs/AI_LOG.md. -->
- [ ] No holdouts, `.league/` contents or secrets touched or printed
- [ ] No target-specific logic in `prm/`
