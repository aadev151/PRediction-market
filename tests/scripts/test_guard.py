import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "guard.py"
_spec = importlib.util.spec_from_file_location("guard", SCRIPT)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

REPO = "/work/repo"


def blocked(tool: str, **tool_input) -> str | None:
    return guard.evaluate(tool, tool_input, REPO)


# ---------------------------------------------------------------------------------- holdouts / league


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("Read", {"file_path": ".league/labels.json"}),  # PLAN §9 row H done-criterion
        ("Read", {"file_path": "/work/repo/.league/labels.json"}),
        ("Read", {"file_path": "prm/../.LEAGUE/x"}),
        ("Read", {"file_path": ".prm-cache/holdouts/test_a.py"}),
        ("Read", {"file_path": "/tmp/acme-holdouts/test_auth.py"}),
        ("Read", {"file_path": "examples/../../acme-holdouts/t.py"}),
        ("Grep", {"pattern": "def test", "path": ".league"}),
        ("Grep", {"pattern": "x", "glob": "**/*holdouts*/**"}),
        ("Glob", {"pattern": "**/.prm-cache/**"}),
        ("Glob", {"pattern": "*", "path": "/srv/holdouts"}),
        ("Write", {"file_path": ".league/labels.json", "content": "{}"}),
        ("Bash", {"command": "cat .league/labels.json"}),
        ("Bash", {"command": "ls -la .prm-cache"}),
        ("Bash", {"command": "cp -r ../acme-holdouts /tmp/x"}),
        ("Bash", {"command": "cd examples && cat ../../acme-holdouts/t.py"}),
        ("Bash", {"command": "python3 -c \"print(open('.league/x').read())\""}),
        ("Bash", {"command": "python3 - <<'EOF'\nprint(open('/srv/holdouts/a.py').read())\nEOF"}),
    ],
)
def test_blocks_holdouts_and_league(tool, tool_input):
    assert guard.evaluate(tool, tool_input, REPO)


# -------------------------------------------------------------------------------------------- secrets


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("Read", {"file_path": ".env"}),
        ("Read", {"file_path": "/work/repo/.env.local"}),
        ("Grep", {"pattern": "TOKEN", "path": ".env"}),
        ("Edit", {"file_path": ".env", "old_string": "a", "new_string": "b"}),
        ("Bash", {"command": "cat .env"}),
        ("Bash", {"command": "source .env && make dev"}),
        ("Bash", {"command": "grep TOKEN .env.production"}),
        ("Bash", {"command": "echo $HOLDOUTS_TOKEN"}),
        ("Bash", {"command": 'curl -H "Authorization: token ${GITHUB_TOKEN}" https://api.github.com'}),
        ("Bash", {"command": "echo ${#SETTLE_HMAC_SECRET}"}),
        ("Bash", {"command": "printenv"}),
        ("Bash", {"command": "printenv GITHUB_WEBHOOK_SECRET"}),
        ("Bash", {"command": "env | grep TOKEN"}),
        ("Bash", {"command": "env"}),
        ("Bash", {"command": "set"}),
        ("Bash", {"command": "export -p"}),
        ("Bash", {"command": "cat /proc/self/environ"}),
        ("Bash", {"command": "gh auth token"}),
        ("Bash", {"command": "gh auth status --show-token"}),
        ("Bash", {"command": "python3 -c \"import os; print(os.environ['HOLDOUTS_TOKEN'])\""}),
        ("Bash", {"command": "node -e 'console.log(process.env.ANTHROPIC_API_KEY)'"}),
        ("Bash", {"command": "python3 - <<'EOF'\nimport os\nprint(os.getenv('PRM_ADMIN_TOKEN'))\nEOF"}),
    ],
)
def test_blocks_secret_reads(tool, tool_input):
    assert guard.evaluate(tool, tool_input, REPO)


# ------------------------------------------------------------------------------- protected config


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("Edit", {"file_path": ".github/workflows/ci.yml", "old_string": "a", "new_string": "b"}),
        ("Write", {"file_path": "/work/repo/.semgrep/rules.yml", "content": ""}),
        ("Write", {"file_path": ".semgrepignore", "content": "prm/"}),
        ("MultiEdit", {"file_path": ".claude/settings.json", "edits": []}),
        ("Write", {"file_path": ".githooks/pre-commit", "content": "exit 0"}),
        ("NotebookEdit", {"notebook_path": ".github/workflows/x.ipynb", "new_source": ""}),
        ("Edit", {"file_path": "scripts/guard.py", "old_string": "a", "new_string": "b"}),
        ("Write", {"file_path": "~/.claude/settings.json", "content": "{}"}),
        ("Bash", {"command": "echo 'on: push' > .github/workflows/x.yml"}),
        ("Bash", {"command": "cat >> .semgrep/rules.yml <<'EOF'\nrules: []\nEOF"}),
        ("Bash", {"command": "sed -i '' 's/--error//' .github/workflows/ci.yml"}),
        ("Bash", {"command": "rm -rf .claude"}),
        ("Bash", {"command": "mv .githooks /tmp/hooks"}),
        ("Bash", {"command": "git rm .semgrep/rules.yml"}),
        ("Bash", {"command": "cp /tmp/empty.yml .github/workflows/ci.yml"}),
        ("Bash", {"command": "echo '{}' | tee .claude/settings.json"}),
        ("Bash", {"command": "chmod -x scripts/git_guard.py"}),
    ],
)
def test_blocks_editing_ci_semgrep_and_hooks(tool, tool_input):
    assert guard.evaluate(tool, tool_input, REPO)


# --------------------------------------------------------------------------- deleting / skipping tests


@pytest.mark.parametrize(
    "cmd",
    [
        "rm tests/test_config.py",
        "rm -rf tests",
        "git rm -q tests/market/test_lmsr.py",
        "rm prm/test_helpers_test.py",
        "find tests -name '*.py' -delete",
        "git commit --no-verify -m wip",
        "npm publish --no-verify",
        "pytest -q --deselect tests/test_config.py::test_x",
        'pytest -k "not lmsr"',
        "pytest -k 'slow and not db'",
        "PRM_FAKE_LLM=1 .venv/bin/pytest -q --ignore=tests/market",
        "python -m pytest --ignore tests/market",
    ],
)
def test_blocks_test_deletion_and_skips(cmd):
    assert blocked("Bash", command=cmd)


# -------------------------------------------------------------------------------------------- allowed


@pytest.mark.parametrize(
    ("tool", "tool_input"),
    [
        ("Read", {"file_path": "prm/market/lmsr.py"}),
        ("Read", {"file_path": "examples/sample-target-holdouts/test_x.py"}),
        ("Read", {"file_path": "/work/repo/examples/sample-target-holdouts/test_x.py"}),
        ("Read", {"file_path": ".env.example"}),
        ("Read", {"file_path": ".github/workflows/ci.yml"}),  # reading CI is fine, editing isn't
        ("Read", {"file_path": "prm/settle/holdouts.py"}),  # factory source, not a holdouts dir
        ("Bash", {"command": "pytest -q tests/settle/test_holdouts.py"}),
        ("Grep", {"pattern": "holdouts_path|HOLDOUTS_TOKEN", "path": "prm"}),
        ("Grep", {"pattern": ".league", "glob": "*.md"}),
        ("Glob", {"pattern": "examples/sample-target-holdouts/**/*.py"}),
        ("Edit", {"file_path": "prm/server.py", "old_string": "a", "new_string": "b"}),
        ("Write", {"file_path": "tests/market/test_lmsr.py", "content": "def test_x(): pass"}),
        ("Write", {"file_path": "~/.claude/projects/x/memory/notes.md", "content": "hi"}),
        ("Bash", {"command": "cat .env.example"}),
        ("Bash", {"command": "pytest -q"}),
        ("Bash", {"command": "PRM_FAKE_LLM=1 .venv/bin/pytest -q -k lmsr"}),
        ("Bash", {"command": "git rm prm/old.py"}),
        ("Bash", {"command": "rm -rf .pytest_cache build"}),
        ("Bash", {"command": "make check && git push -u origin wp-h/guard-hook"}),
        ("Bash", {"command": 'git commit -m "WP D: sync holdouts into .prm-cache; never read .env"'}),
        ("Bash", {"command": "gh pr create --body 'read examples/sample-target-holdouts/ only'"}),
        ("Bash", {"command": "cat > docs/N.md <<'EOF'\nHOLDOUTS_TOKEN is in .env, see .league\nEOF"}),
        ("Bash", {"command": "cd examples && ls sample-target-holdouts"}),
        ("Bash", {"command": "grep -rn GITHUB_TOKEN prm/ .env.example"}),
        ("Bash", {"command": "env PRM_FAKE_LLM=1 pytest -q"}),
        ("Bash", {"command": "set -euo pipefail; ruff check ."}),
        ("Bash", {"command": "cp .github/workflows/ci.yml /tmp/ci-copy.yml"}),
        ("Bash", {"command": "python3 -c 'import os; print(os.environ.get(\"PRM_DB\"))'"}),
        ("WebFetch", {"url": "https://example.com/.league"}),
    ],
)
def test_allows_normal_work(tool, tool_input):
    assert guard.evaluate(tool, tool_input, REPO) is None


# ------------------------------------------------------------------------------------ hook protocol


def _run(stdin: str, python: str = sys.executable) -> subprocess.CompletedProcess:
    return subprocess.run([python, str(SCRIPT)], input=stdin, capture_output=True, text=True)


def _payload(tool: str, **tool_input) -> str:
    return json.dumps({"tool_name": tool, "tool_input": tool_input, "cwd": REPO})


def test_hook_blocks_league_read_with_exit_2():
    res = _run(_payload("Read", file_path=".league/labels.json"))
    assert res.returncode == 2
    assert "BLOCKED" in res.stderr and ".league" in res.stderr


def test_hook_allows_with_exit_0_and_no_output():
    res = _run(_payload("Read", file_path="prm/market/lmsr.py"))
    assert res.returncode == 0 and res.stdout == "" and res.stderr == ""


@pytest.mark.parametrize("stdin", ["", "not json", "[1, 2]", '"str"'])
def test_unparseable_payload_is_allowed_silently(stdin):
    res = _run(stdin)
    assert res.returncode == 0 and res.stderr == ""


def test_odd_shapes_do_not_crash():
    assert _run(json.dumps({"tool_name": "Read", "tool_input": "oops"})).returncode == 0
    assert _run(json.dumps({"tool_name": "Bash"})).returncode == 0


def test_internal_error_fails_closed(monkeypatch, capsys):
    def boom(*_args):
        raise RuntimeError("bug")

    monkeypatch.setattr(guard, "evaluate", boom)
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(_payload("Read", file_path="x")))
    assert guard.main() == 2
    assert "guard error" in capsys.readouterr().err


def test_never_echoes_secret_values(monkeypatch):
    res = _run(_payload("Bash", command="echo $GITHUB_TOKEN"))
    assert res.returncode == 2 and "$GITHUB_TOKEN" not in res.stderr


@pytest.mark.skipif(not Path("/usr/bin/python3").exists(), reason="no system python3")
def test_runs_under_system_python():
    """The hook runs under whatever python3 is on PATH (macOS: 3.9)."""
    assert _run(_payload("Read", file_path=".league/x"), python="/usr/bin/python3").returncode == 2
    assert _run(_payload("Read", file_path="prm/types.py"), python="/usr/bin/python3").returncode == 0
