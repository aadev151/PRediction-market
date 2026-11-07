"""The league oracle: hidden tests on a mutant tree, counts only, scrubbed env, fail-closed on infra."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from prm.config import load_target_config
from prm.league.oracle import parse_junit_counts, run_tests
from prm.pipeline import apply_patch
from tests.league.sample import BUG_PATCH


def test_clean_sample_target_passes(sample_copy: Path) -> None:
    r = run_tests(sample_copy, load_target_config(sample_copy), sample_copy)
    assert r.error is None
    assert r.outcome == "YES"
    assert r.holdouts_total > 0
    assert r.holdouts_passed == r.holdouts_total
    assert r.findings == []


def test_bug_auth_patch_fails(sample_copy: Path) -> None:
    apply_patch(sample_copy, BUG_PATCH)
    r = run_tests(sample_copy, load_target_config(sample_copy), sample_copy)
    assert r.error is None
    assert r.outcome == "NO"
    assert r.holdouts_total > 0
    assert r.holdouts_passed < r.holdouts_total


def test_missing_hidden_tests_is_infra_error(tmp_path: Path, sample_copy: Path) -> None:
    target = load_target_config(sample_copy)
    shutil.rmtree(target.holdouts_dir(sample_copy))
    r = run_tests(sample_copy, target, sample_copy)
    assert r.outcome is None
    assert r.error == "hidden tests not found"


def _tiny_target(root: Path, command: str, timeout_s: int = 60) -> Path:
    (root / "suite").mkdir(parents=True)
    target = root / "tiny"
    target.mkdir()
    (target / ".prm.toml").write_text(
        "[target]\n"
        'name = "tiny"\n'
        'holdouts_path = "../suite"\n'
        "[settle]\n"
        f"command = '''{command}'''\n"
        f"timeout_s = {timeout_s}\n",
        encoding="utf-8",
    )
    return target


def test_timeout_is_no(tmp_path: Path) -> None:
    target = _tiny_target(tmp_path, 'python -c "import time; time.sleep(30)"', timeout_s=1)
    r = run_tests(target, load_target_config(target), target)
    assert r.outcome == "NO"
    assert r.error is None


def test_env_is_scrubbed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-fake-should-not-leak")
    monkeypatch.setenv("GITHUB_TOKEN", "ghp-fake-should-not-leak")
    cmd = "python -c \"import os, json; open('env.json', 'w').write(json.dumps(sorted(os.environ)))\""
    target = _tiny_target(tmp_path, cmd)
    r = run_tests(target, load_target_config(target), target)
    keys = set(json.loads((target / "env.json").read_text(encoding="utf-8")))
    assert "ANTHROPIC_API_KEY" not in keys
    assert "GITHUB_TOKEN" not in keys
    assert {"PATH", "HOME", "TMPDIR"} <= keys
    assert r.outcome == "YES"  # no JUnit file: the exit code decides
    assert (r.holdouts_passed, r.holdouts_total) == (0, 0)


def test_failing_exit_without_junit_is_no(tmp_path: Path) -> None:
    target = _tiny_target(tmp_path, 'python -c "raise SystemExit(3)"')
    assert run_tests(target, load_target_config(target), target).outcome == "NO"


def test_unknown_executable_is_infra_error(tmp_path: Path) -> None:
    target = _tiny_target(tmp_path, "prm-no-such-binary-xyz")
    r = run_tests(target, load_target_config(target), target)
    assert r.outcome is None and r.error


def test_parse_junit_counts() -> None:
    xml = (
        '<?xml version="1.0"?><testsuites name="x">'
        '<testsuite name="a" errors="1" failures="2" skipped="0" tests="10" time="1">'
        '<testcase name="t"/></testsuite>'
        '<testsuite name="b" tests="5" failures="0" errors="0"></testsuite></testsuites>'
    )
    assert parse_junit_counts(xml) == (15, 3)
    assert parse_junit_counts("<testsuites></testsuites>") is None
    assert parse_junit_counts("garbage") is None
