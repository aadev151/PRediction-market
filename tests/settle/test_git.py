from pathlib import Path

import pytest

from prm.settle.git import Git, GitError


def test_env_carries_no_factory_secrets(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-should-not-leak")
    monkeypatch.setenv("PRM_WEBHOOK_SECRET", "should-not-leak")
    git = Git(tmp_path)
    allowed = {"PATH", "LC_ALL", "GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GIT_TERMINAL_PROMPT"}
    assert set(git._env) == allowed
    assert "should-not-leak" not in " ".join(git._env.values())


def test_errors_name_the_subcommand_only(tmp_path: Path) -> None:
    with pytest.raises(GitError) as exc:
        Git(tmp_path / "missing").run("log", "--secret-looking-arg")
    assert str(exc.value).startswith("git log exited ")
    assert "secret" not in str(exc.value) and str(tmp_path) not in str(exc.value)
