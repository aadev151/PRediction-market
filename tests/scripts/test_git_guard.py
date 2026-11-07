import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("git_guard", ROOT / "scripts" / "git_guard.py")
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)


def _repo(path: Path, branch: str) -> Path:
    subprocess.run(["git", "init", "-q", "-b", branch, str(path)], check=True)
    return path


@pytest.fixture
def on_main(tmp_path):
    return str(_repo(tmp_path / "m", "main"))


@pytest.fixture
def on_branch(tmp_path):
    return str(_repo(tmp_path / "b", "wp-a/lmsr"))


@pytest.mark.parametrize(
    "cmd",
    [
        'git commit -m "x"',
        "git add -A && git commit -am wip",
        "git -C . commit -m x",
        "git merge wp-b/signals",
        "git rebase origin/main",
        "git cherry-pick abc123",
        "git push",
        "git push origin",
        "git push origin HEAD",
        "FOO=1 git push -u origin main",
    ],
)
def test_blocks_history_writes_on_main(on_main, cmd):
    assert guard.evaluate(cmd, on_main)


@pytest.mark.parametrize(
    "cmd",
    [
        "git status",
        "git log --oneline -5",
        "git pull --ff-only",
        "git switch -c wp-a/lmsr",
        "git fetch origin",
        "git commit --dry-run",
        "git config --get core.hooksPath",
        "git config core.hooksPath .githooks",
        "echo 'git push origin main'",  # not a git invocation
    ],
)
def test_allows_safe_commands_on_main(on_main, cmd):
    assert guard.evaluate(cmd, on_main) is None


@pytest.mark.parametrize(
    "cmd",
    [
        "git commit -m 'wp-a: lmsr'",
        "git push -u origin HEAD",
        "git push origin wp-a/lmsr",
        "git push",
        "git push --force-with-lease origin wp-a/lmsr:wp-a/lmsr",
    ],
)
def test_allows_branch_work(on_branch, cmd):
    assert guard.evaluate(cmd, on_branch) is None


@pytest.mark.parametrize(
    "cmd",
    [
        "git push origin main",
        "git push origin HEAD:main",
        "git push origin +wp-a/lmsr:refs/heads/main",
        "git push origin --delete main",
        "git push --all origin",
        "git push --mirror origin",
        "git commit --no-verify -m x",
        "git commit -nm x",
        "git push --no-verify",
        "git config core.hooksPath /dev/null",
        "git config --unset core.hooksPath",
        "git -c core.hooksPath=/tmp commit -m x",
    ],
)
def test_blocks_pushes_to_main_and_hook_bypass_from_any_branch(on_branch, cmd):
    assert guard.evaluate(cmd, on_branch)


def test_follows_cd(on_main, tmp_path):
    other = _repo(tmp_path / "other", "feature")
    assert guard.evaluate(f"cd {other} && git commit -m x", on_main) is None
    assert guard.evaluate(f"cd {other} && git push origin main", on_main)


def _run_hook(payload: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "git_guard.py")],
        input=json.dumps(payload),
        capture_output=True,
        text=True,
    )


def test_hook_protocol(on_main):
    blocked = _run_hook({"tool_name": "Bash", "tool_input": {"command": "git push"}, "cwd": on_main})
    assert blocked.returncode == 2 and "BLOCKED" in blocked.stderr
    allowed = _run_hook({"tool_name": "Bash", "tool_input": {"command": "ls"}, "cwd": on_main})
    assert allowed.returncode == 0
    assert _run_hook({"tool_name": "Read", "tool_input": {}}).returncode == 0
    bad = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "git_guard.py")], input="not json", text=True
    )
    assert bad.returncode == 0


# ------------------------------------------------------------------------------------- .githooks/


def _git(repo: Path, *args: str, check: bool = False) -> subprocess.CompletedProcess:
    return subprocess.run(
        [
            "git",
            "-C",
            str(repo),
            "-c",
            "user.name=t",
            "-c",
            "user.email=t@example.com",
            "-c",
            "commit.gpgsign=false",
            *args,
        ],
        capture_output=True,
        text=True,
        check=check,
    )


def test_pre_commit_hook_blocks_main_only(tmp_path):
    repo = _repo(tmp_path / "r", "main")
    _git(repo, "config", "core.hooksPath", str(ROOT / ".githooks"), check=True)
    (repo / "f.txt").write_text("x")
    _git(repo, "add", "f.txt", check=True)
    res = _git(repo, "commit", "-m", "on main")
    assert res.returncode != 0 and "not allowed" in res.stderr
    _git(repo, "switch", "-q", "-c", "wp-a/x", check=True)
    assert _git(repo, "commit", "-m", "on branch").returncode == 0


@pytest.mark.parametrize(
    ("remote_ref", "ok"),
    [("refs/heads/main", False), ("refs/heads/master", False), ("refs/heads/wp-a/x", True)],
)
def test_pre_push_hook(remote_ref, ok):
    line = f"refs/heads/wp-a/x {'a' * 40} {remote_ref} {'0' * 40}\n"
    res = subprocess.run(
        [str(ROOT / ".githooks" / "pre-push"), "origin", "url"], input=line, capture_output=True, text=True
    )
    assert (res.returncode == 0) is ok
