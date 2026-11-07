"""WP D. The post-merge oracle (DECISIONS #26, R-SET-0).

A merged PR settles **NO** as soon as a later commit on the branch, within ``window_days`` of the merge,
reverts it or is a fix commit that changes or deletes a line the PR added. Otherwise it settles **YES** once
the window has elapsed, and until then it is pending. The clock is a parameter (``now``), so a demo can
fast-forward the window by passing ``now=verdict.settles_at``.

Reads a local clone of the target with read-only git commands: ``shell=False``, no external diff or textconv,
no user/system git config, and an environment without factory secrets. Any git failure raises ``GitError``;
``settle_postmerge`` turns that into an infra error, which VOIDs the market (fail closed).
"""

from __future__ import annotations

import codecs
import os
import re
import shutil
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from prm.types import SettlementResult

DEFAULT_WINDOW_DAYS = 14
GIT_TIMEOUT_S = 60
# What makes a later commit that touches the PR's lines a "fix". A refactor or feature that edits them is not.
FIX_PATTERN = re.compile(r"\b(fix(e[sd])?|bug(fix)?|hotfix|revert(s|ed)?|regression)\b", re.IGNORECASE)

Status = Literal["pending", "yes", "no"]
Reason = Literal["in_window", "window_elapsed", "reverted", "fixed"]

_SHA = re.compile(r"[0-9a-f]{7,40}")
_REF = re.compile(r"[A-Za-z0-9_][A-Za-z0-9_./-]*")
_REVERTS = re.compile(r"This reverts commit ([0-9a-f]{7,40})")
_HUNK = re.compile(r"@@ -(\d+)(?:,(\d+))? \+")
_BLAME_HEADER = re.compile(r"([0-9a-f]{40}) \d+ \d+")


class GitError(RuntimeError):
    """git failed or the history doesn't fit the merge we were given. VOIDs the market."""


@dataclass(frozen=True)
class MergedPR:
    """A merge on the target branch. The PR's own commits are ``base_sha..merge_sha`` (default: merge^1)."""

    merge_sha: str
    merged_at: datetime
    base_sha: str | None = None

    def __post_init__(self) -> None:
        for sha in (self.merge_sha, self.base_sha):
            if sha is not None and not _SHA.fullmatch(sha):
                raise ValueError("commit ids must be 7-40 lowercase hex characters")
        if self.merged_at.tzinfo is None:
            raise ValueError("merged_at must be timezone-aware")


@dataclass(frozen=True)
class Verdict:
    status: Status
    reason: Reason
    settles_at: datetime  # end of the window: when a quiet PR settles YES
    culprit_sha: str | None = None  # the revert or fix commit, for a NO
    observed_at: datetime | None = None

    def to_settlement(self, duration_ms: int = 0) -> SettlementResult | None:
        """The engine's input once final; None while pending. No holdouts here, so the counts are 0/0."""
        if self.status == "pending":
            return None
        return SettlementResult("YES" if self.status == "yes" else "NO", 0, 0, [], duration_ms, None)


def window_end(merged_at: datetime, window_days: int = DEFAULT_WINDOW_DAYS) -> datetime:
    return merged_at + timedelta(days=window_days)


def evaluate(
    repo: str | os.PathLike[str],
    pr: MergedPR,
    *,
    now: datetime,
    branch: str = "HEAD",
    window_days: int = DEFAULT_WINDOW_DAYS,
    fix_pattern: re.Pattern[str] | None = FIX_PATTERN,
) -> Verdict:
    """Judge ``pr`` as of ``now`` on ``branch``'s history. ``fix_pattern=None`` counts any touch as a fix."""
    if now.tzinfo is None:
        raise ValueError("now must be timezone-aware")
    if window_days <= 0:
        raise ValueError("window_days must be positive")
    if not _REF.fullmatch(branch) or ".." in branch:
        raise ValueError("invalid branch name")
    deadline = window_end(pr.merged_at, window_days)
    cutoff = min(now, deadline)
    git = _Git(Path(repo))
    merge = git.out("rev-parse", "--verify", "--quiet", f"{pr.merge_sha}^{{commit}}").strip()
    base = git.out("rev-parse", "--verify", "--quiet", f"{pr.base_sha or merge + '^1'}^{{commit}}").strip()
    if git.run("merge-base", "--is-ancestor", merge, branch, check=False).returncode != 0:
        raise GitError("the merge commit is not on the branch")
    pr_commits = set(git.out("rev-list", f"{base}..{merge}").split())

    later = git.out("log", "--no-merges", "--format=%H%x1f%ct%x1f%B%x1e", f"{merge}..{branch}")
    commits = []
    for record in later.split("\x1e"):
        if record.strip():
            sha, ts, message = record.strip("\n").split("\x1f", 2)
            commits.append((datetime.fromtimestamp(int(ts), tz=pr.merged_at.tzinfo), sha, message))
    for when, sha, message in sorted(commits):
        if when > cutoff:
            continue
        if any(c.startswith(ref) for ref in _REVERTS.findall(message) for c in pr_commits):
            return Verdict("no", "reverted", deadline, sha, when)
        if (fix_pattern is None or fix_pattern.search(message)) and _touches(git, sha, pr_commits):
            return Verdict("no", "fixed", deadline, sha, when)
    if now >= deadline:
        return Verdict("yes", "window_elapsed", deadline)
    return Verdict("pending", "in_window", deadline)


def settle_postmerge(
    repo: str | os.PathLike[str],
    pr: MergedPR,
    *,
    now: datetime,
    branch: str = "HEAD",
    window_days: int = DEFAULT_WINDOW_DAYS,
) -> SettlementResult | None:
    """The engine-facing oracle: a final result, None while pending, or an infra error (VOID) on failure."""
    start = time.monotonic()
    try:
        verdict = evaluate(repo, pr, now=now, branch=branch, window_days=window_days)
    except (GitError, ValueError) as exc:
        return SettlementResult.infra_error(f"post-merge settlement failed: {exc}", _ms_since(start))
    return verdict.to_settlement(_ms_since(start))


def _ms_since(start: float) -> int:
    return int((time.monotonic() - start) * 1000)


def _touches(git: _Git, sha: str, pr_commits: set[str]) -> bool:
    """Does ``sha`` change or delete a line that, in its parent, was last written by one of ``pr_commits``?"""
    diff = git.out(
        "diff", "-U0", "--no-color", "--no-ext-diff", "--no-textconv", "--find-renames",
        "--src-prefix=a/", "--dst-prefix=b/", f"{sha}^", sha,
    )  # fmt: skip
    ranges: dict[str, list[str]] = {}
    path: str | None = None
    in_header = False
    for line in diff.splitlines():
        if line.startswith("diff --git "):
            in_header, path = True, None
        elif in_header and line.startswith("--- "):
            old = _unquote(line[4:])
            path = old[2:] if old.startswith("a/") else None  # /dev/null: a new file has no old lines
        elif line.startswith("@@"):
            in_header = False
            hunk = _HUNK.match(line)
            count = 1 if hunk is None or hunk.group(2) is None else int(hunk.group(2))
            if hunk and path and count > 0:
                ranges.setdefault(path, []).extend(["-L", f"{hunk.group(1)},+{count}"])
    for old_path, line_args in ranges.items():
        blame = git.out("blame", "--porcelain", *line_args, f"{sha}^", "--", old_path)
        for row in blame.splitlines():
            header = _BLAME_HEADER.match(row)
            if header and header.group(1) in pr_commits:
                return True
    return False


def _unquote(path: str) -> str:
    """Undo git's C-style quoting of unusual path names ("a/caf\\303\\251.py")."""
    if len(path) >= 2 and path[0] == path[-1] == '"':
        return codecs.escape_decode(path[1:-1].encode("utf-8"))[0].decode("utf-8", errors="surrogateescape")
    return path


class _Git:
    def __init__(self, repo: Path) -> None:
        exe = shutil.which("git")
        if exe is None:
            raise GitError("git is not installed")
        self._base = [exe, "-C", str(repo), "-c", "core.quotePath=false", "--no-pager"]
        # No factory secrets, no user/system config (aliases, pagers, external tools), never prompt.
        self._env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
        }

    def run(self, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
        try:
            proc = subprocess.run(  # noqa: S603 (fixed argv, shell=False, validated shas and refs)
                [*self._base, *args], capture_output=True, encoding="utf-8", errors="replace",
                env=self._env, timeout=GIT_TIMEOUT_S, check=False,
            )  # fmt: skip
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitError(f"git {args[0]}: {type(exc).__name__}") from exc
        if check and proc.returncode != 0:
            raise GitError(f"git {args[0]} exited {proc.returncode}")
        return proc

    def out(self, *args: str) -> str:
        return self.run(*args).stdout
