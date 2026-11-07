"""WP D. A local mirror of each target repo for the post-merge oracle, and the one call the pipeline makes.

``ensure_repo("owner/repo", cache_dir)`` keeps a bare clone in ``<cache_dir>/repos/<owner>__<repo>`` and
fetches its branches. A private repo authenticates with ``GITHUB_TOKEN`` (read here, at its point of use)
through an HTTP header passed in git's environment: the token never appears in argv, the clone URL, the
repo's config or an error.

``settle_merged(...)`` = ``ensure_repo`` + ``settle_postmerge``: a final ``SettlementResult``, ``None``
while the window is open, or an infra error (VOID) if anything fails.
"""

from __future__ import annotations

import base64
import fcntl
import os
import re
import shutil
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from prm.settle.git import Git, GitError
from prm.settle.postmerge import DEFAULT_WINDOW_DAYS, MergedPR, settle_postmerge
from prm.types import SettlementResult

GITHUB = "https://github.com/"
CLONE_TIMEOUT_S = 600
FETCH_TIMEOUT_S = 300

_REPO_NAME = re.compile(r"[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}")
_thread_locks: dict[Path, threading.Lock] = {}
_thread_locks_guard = threading.Lock()


def repo_dir(full_name: str, cache_dir: str | os.PathLike[str]) -> Path:
    if not _REPO_NAME.fullmatch(full_name) or any(p in {".", ".."} for p in full_name.split("/")):
        raise ValueError("repo must look like 'owner/repo'")
    owner, name = full_name.split("/")
    return Path(cache_dir) / "repos" / f"{owner}__{name}"


def ensure_repo(full_name: str, cache_dir: str | os.PathLike[str], *, remote_url: str | None = None) -> Path:
    """Clone (first time) or fetch every branch of ``full_name``; returns the bare clone's path.

    ``remote_url`` overrides the GitHub URL (tests, mirrors). Raises ``GitError``/``ValueError`` on failure.
    """
    dest = repo_dir(full_name, cache_dir)
    url = remote_url or f"{GITHUB}{full_name}.git"
    env = _auth_env() if remote_url is None else {}
    dest.parent.mkdir(parents=True, exist_ok=True)
    with _locked(dest):
        if not (dest / "HEAD").is_file():
            _clone(url, dest, env)
        else:
            Git(dest, env).run(
                "fetch", "--quiet", "--prune", "--no-tags", "origin", "+refs/heads/*:refs/heads/*",
                timeout_s=FETCH_TIMEOUT_S,
            )  # fmt: skip
    return dest


def settle_merged(
    full_name: str,
    pr: MergedPR,
    *,
    now: datetime,
    branch: str,
    cache_dir: str | os.PathLike[str],
    window_days: int = DEFAULT_WINDOW_DAYS,
    remote_url: str | None = None,
) -> SettlementResult | None:
    """Sync the target, then judge ``pr`` on ``branch`` (the PR's base branch) as of ``now``."""
    start = time.monotonic()
    try:
        path = ensure_repo(full_name, cache_dir, remote_url=remote_url)
    except (GitError, ValueError) as exc:
        return SettlementResult.infra_error(f"post-merge settlement failed: {exc}", _ms_since(start))
    except OSError as exc:
        return SettlementResult.infra_error(f"post-merge settlement failed: {type(exc).__name__}")
    return settle_postmerge(path, pr, now=now, branch=branch, window_days=window_days)


def _ms_since(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _auth_env() -> dict[str, str]:
    """Only https to github.com; with a token, an Authorization header scoped to github.com."""
    env = {"GIT_ALLOW_PROTOCOL": "https"}
    token = os.environ.get("GITHUB_TOKEN")
    if token:
        basic = base64.b64encode(f"x-access-token:{token}".encode()).decode("ascii")
        env |= {
            "GIT_CONFIG_COUNT": "1",
            "GIT_CONFIG_KEY_0": f"http.{GITHUB}.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
        }
    return env


def _clone(url: str, dest: Path, env: dict[str, str]) -> None:
    """Clone into a temp dir next to ``dest`` and rename it in, so a failed clone never leaves a half repo."""
    tmp = dest.parent / f".{dest.name}.tmp-{uuid.uuid4().hex}"
    try:
        Git(dest.parent, env).run(
            "clone", "--bare", "--quiet", "--no-tags", "--", url, str(tmp), timeout_s=CLONE_TIMEOUT_S
        )
        if dest.exists():
            shutil.rmtree(dest)  # a leftover without HEAD: not a usable clone
        tmp.rename(dest)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@contextmanager
def _locked(dest: Path) -> Iterator[None]:
    """One sync per repo at a time, across threads (thread lock) and server processes (flock)."""
    with _thread_locks_guard:
        lock = _thread_locks.setdefault(dest, threading.Lock())
    with lock, open(dest.parent / f".{dest.name}.lock", "a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
