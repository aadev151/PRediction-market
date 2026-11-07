"""WP E. Verify X-Hub-Signature-256, filter events, dedupe, schedule the pipeline. PLAN §6.12.

Pure functions plus a small in-memory deduper; the HTTP route lives in ``prm.server``. Nothing here logs or
returns the payload body or the secret.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

SIGNATURE_PREFIX = "sha256="
HANDLED_ACTIONS = frozenset({"opened", "synchronize", "reopened"})

REPO_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,100}/[A-Za-z0-9_.-]{1,100}$")
_SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")  # sha1 or sha256 object ids
_HEX_DIGEST = re.compile(r"^[0-9a-fA-F]{64}$")


def valid_repo_name(name: str) -> bool:
    """``owner/name`` with GitHub's characters, and no ``.``/``..`` segment."""
    return bool(REPO_NAME.match(name)) and not any(part in {".", ".."} for part in name.split("/"))


def verify_signature(secret: bytes, body: bytes, header: str | None) -> bool:
    """Check GitHub's ``X-Hub-Signature-256: sha256=<hex>`` in constant time. Missing/malformed -> False.

    An empty secret never verifies: fail closed instead of accepting an HMAC keyed with nothing.
    """
    if not secret or not header or not header.startswith(SIGNATURE_PREFIX):
        return False
    presented = header[len(SIGNATURE_PREFIX) :]
    if not _HEX_DIGEST.match(presented):
        return False
    expected = hmac.new(secret, body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected.encode("ascii"), presented.lower().encode("ascii"))


@dataclass(frozen=True)
class PREvent:
    repo: str
    pr_number: int
    head_sha: str
    base_sha: str
    action: str


def _get(obj: Any, *keys: str) -> Any:
    for key in keys:
        if not isinstance(obj, dict):
            return None
        obj = obj.get(key)
    return obj


def classify_pr_event(event_name: str, payload: Any) -> PREvent | str:
    """The ``PREvent`` to price, or a short reason it is ignored (safe to return to GitHub and to log)."""
    if event_name != "pull_request":
        return "event not handled"
    action = _get(payload, "action")
    if action not in HANDLED_ACTIONS:
        return "action not handled"
    pr = _get(payload, "pull_request")
    if not isinstance(pr, dict):
        return "malformed payload"
    if pr.get("draft") is True:
        return "draft pull request"
    repo = _get(payload, "repository", "full_name")
    if not isinstance(repo, str) or not valid_repo_name(repo):
        return "malformed payload"
    head_repo = _get(pr, "head", "repo", "full_name")
    if not isinstance(head_repo, str) or head_repo != repo:
        return "fork pull request"
    number, head_sha, base_sha = pr.get("number"), _get(pr, "head", "sha"), _get(pr, "base", "sha")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        return "malformed payload"
    if not (isinstance(head_sha, str) and _SHA.match(head_sha)):
        return "malformed payload"
    if not (isinstance(base_sha, str) and _SHA.match(base_sha)):
        return "malformed payload"
    return PREvent(repo=repo, pr_number=number, head_sha=head_sha, base_sha=base_sha, action=action)


@dataclass(frozen=True)
class MergeEvent:
    repo: str
    pr_number: int
    head_sha: str
    merge_sha: str
    merged_at: datetime
    branch: str


_BRANCH = re.compile(r"^[A-Za-z0-9._/-]{1,255}$")


def classify_merge_event(event_name: str, payload: Any) -> MergeEvent | str:
    """A merged, same-repo ``pull_request`` ``closed`` event, or a short reason it isn't one."""
    if event_name != "pull_request" or _get(payload, "action") != "closed":
        return "not a merge"
    pr = _get(payload, "pull_request")
    if not isinstance(pr, dict) or pr.get("merged") is not True:
        return "closed without merging"
    repo = _get(payload, "repository", "full_name")
    if (
        not isinstance(repo, str)
        or not valid_repo_name(repo)
        or _get(pr, "head", "repo", "full_name") != repo
    ):
        return "fork or malformed pull request"
    number, head_sha, merge_sha = pr.get("number"), _get(pr, "head", "sha"), pr.get("merge_commit_sha")
    branch, merged_at = _get(pr, "base", "ref"), pr.get("merged_at")
    if isinstance(number, bool) or not isinstance(number, int) or number <= 0:
        return "malformed payload"
    if not all(isinstance(s, str) and _SHA.match(s) for s in (head_sha, merge_sha)):
        return "malformed payload"
    if not (isinstance(branch, str) and _BRANCH.match(branch) and ".." not in branch):
        return "malformed payload"
    try:
        when = datetime.fromisoformat(str(merged_at).replace("Z", "+00:00"))
    except ValueError:
        return "malformed payload"
    if when.tzinfo is None:
        return "malformed payload"
    return MergeEvent(repo, number, head_sha, merge_sha, when.astimezone(UTC), branch)


def parse_pr_event(event_name: str, payload: dict) -> PREvent | None:
    """A ``pull_request`` opened/synchronize/reopened event from a non-draft, same-repo PR; else None."""
    result = classify_pr_event(event_name, payload)
    return result if isinstance(result, PREvent) else None


class Deduper:
    """Remembers the last ``maxsize`` (repo, pr, sha) keys. Thread-safe; the oldest key is evicted first.

    In-memory only: GitHub redeliveries within one process are dropped, and the engine's unique index on
    (repo, pr, sha, source, attempt) keeps a restart from opening a second market for the same sha.
    """

    def __init__(self, maxsize: int = 4096) -> None:
        if maxsize < 1:
            raise ValueError("maxsize must be >= 1")
        self._maxsize = maxsize
        self._seen: OrderedDict[tuple[str, int, str], None] = OrderedDict()
        self._lock = threading.Lock()

    def first_time(self, repo: str, pr_number: int, sha: str) -> bool:
        """Record the key; True the first time it is seen, False for a duplicate."""
        key = (repo.lower(), pr_number, sha)
        with self._lock:
            if key in self._seen:
                self._seen.move_to_end(key)
                return False
            self._seen[key] = None
            if len(self._seen) > self._maxsize:
                self._seen.popitem(last=False)
            return True

    def forget(self, repo: str, pr_number: int, sha: str) -> None:
        """Drop a key, e.g. when scheduling failed and a redelivery should be accepted."""
        with self._lock:
            self._seen.pop((repo.lower(), pr_number, sha), None)

    def __len__(self) -> int:
        with self._lock:
            return len(self._seen)
