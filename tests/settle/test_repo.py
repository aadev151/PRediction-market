import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest

from prm.settle import repo as repo_mod
from prm.settle.git import Git, GitError
from prm.settle.postmerge import MergedPR, window_end
from prm.settle.repo import ensure_repo, repo_dir, settle_merged
from tests.settle.gitrepo import BASE, PR_ADDS, T0, Repo


@pytest.fixture
def origin(tmp_path: Path) -> Repo:
    (tmp_path / "origin").mkdir()
    return Repo(tmp_path / "origin")


def test_first_call_clones_bare_and_later_calls_fetch(origin: Repo, tmp_path: Path) -> None:
    cache = tmp_path / "cache"
    path = ensure_repo("acme/app", cache, remote_url=str(origin.path))
    assert path == cache / "repos" / "acme__app"
    assert (path / "HEAD").is_file() and not (path / "app.py").exists()  # bare: no checkout
    new = origin.commit({"app.py": BASE + PR_ADDS}, "Add b", T0)
    assert ensure_repo("acme/app", cache, remote_url=str(origin.path)) == path
    assert Git(path).out("rev-parse", "main").strip() == new


def test_settle_merged_end_to_end(origin: Repo, tmp_path: Path) -> None:
    pr = MergedPR(origin.commit({"app.py": BASE + PR_ADDS}, "Add b (#7)", T0), T0)
    kw = {"branch": "main", "cache_dir": tmp_path / "cache", "remote_url": str(origin.path)}
    assert settle_merged("acme/app", pr, now=T0 + timedelta(days=1), **kw) is None  # window open
    fast_forward = settle_merged("acme/app", pr, now=window_end(T0), **kw)
    assert fast_forward is not None and fast_forward.outcome == "YES"
    origin.edit("return 2", "return 3", "fix b", T0 + timedelta(days=2))  # seen on the next sync
    result = settle_merged("acme/app", pr, now=T0 + timedelta(days=3), **kw)
    assert result is not None and result.outcome == "NO"


def test_failures_are_infra_errors_and_leave_no_half_clone(tmp_path: Path) -> None:
    pr = MergedPR("a" * 40, T0)
    missing = str(tmp_path / "no-such-remote")
    kw = {"now": T0, "branch": "main", "cache_dir": tmp_path / "c"}
    result = settle_merged("acme/app", pr, remote_url=missing, **kw)
    assert result is not None and result.outcome is None and result.error
    assert missing not in result.error  # no URLs in errors
    assert [p.name for p in (tmp_path / "c" / "repos").iterdir()] == [".acme__app.lock"]
    bad = settle_merged("../etc", pr, **kw)
    assert bad is not None and bad.outcome is None


@pytest.mark.parametrize("name", ["", "acme", "acme/app/x", "../app", "acme/..", "acme/a b"])
def test_repo_names_are_validated(name: str, tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        repo_dir(name, tmp_path)


def test_concurrent_first_syncs_share_one_clone(origin: Repo, tmp_path: Path) -> None:
    def sync(_: int) -> Path:
        return ensure_repo("acme/app", tmp_path / "cache", remote_url=str(origin.path))

    with ThreadPoolExecutor(4) as pool:
        assert len(set(pool.map(sync, range(4)))) == 1


def test_github_mode_is_https_only(origin: Repo, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repo_mod, "GITHUB", f"{origin.path.parent}/")  # a local path posing as the host
    (origin.path.parent / "acme").mkdir()
    (origin.path.parent / "acme" / "app.git").symlink_to(origin.path)
    with pytest.raises(GitError):
        ensure_repo("acme/app", tmp_path / "cache")


def test_token_goes_in_a_github_scoped_header_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert repo_mod._auth_env() == {"GIT_ALLOW_PROTOCOL": "https"}
    monkeypatch.setenv("GITHUB_TOKEN", "test-value-123")
    env = repo_mod._auth_env()
    assert env["GIT_CONFIG_KEY_0"] == "http.https://github.com/.extraheader"
    encoded = base64.b64encode(b"x-access-token:test-value-123").decode()
    assert env["GIT_CONFIG_VALUE_0"] == f"AUTHORIZATION: basic {encoded}"
    assert not any("test-value-123" in v for v in env.values())
