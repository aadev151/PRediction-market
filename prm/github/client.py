"""WP E. httpx GitHub client: PR, diff, file at sha, safe tarball extraction, statuses. PLAN §6.11.

The token is read from ``GITHUB_TOKEN`` at call time unless one is passed in. It never appears in a repr, a
log line or an error: errors carry the HTTP method, the API path and the status code only.
"""

from __future__ import annotations

import io
import logging
import os
import re
import shutil
import tarfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal
from urllib.parse import quote

import httpx

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://api.github.com"
STATUS_CONTEXT = "pr-market"
MAX_DESCRIPTION = 140  # GitHub rejects longer status descriptions
MAX_TARBALL_BYTES = 200 * 1024 * 1024
MAX_EXTRACTED_BYTES = 500 * 1024 * 1024
TIMEOUT_S = 30.0
StatusState = Literal["pending", "success", "failure", "error"]
_STATES = frozenset({"pending", "success", "failure", "error"})
_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_REF = re.compile(r"^[0-9A-Za-z._/-]{1,255}$")


class GitHubError(RuntimeError):
    """A GitHub call failed. The message names the method, path and status only (never headers)."""


class UnsafeArchive(GitHubError):
    """A tarball member would escape the destination (absolute path, ``..``, link or special file)."""


@dataclass(frozen=True)
class PullRequest:
    number: int
    title: str
    body: str
    head_sha: str
    base_sha: str
    head_repo: str | None  # None when the fork was deleted
    base_repo: str
    draft: bool
    labels: tuple[str, ...]


def check_repo(repo: str) -> str:
    """``owner/name`` with no path tricks, or ValueError."""
    if not _REPO.match(repo) or any(part in {".", ".."} for part in repo.split("/")):
        raise ValueError(f"repo must look like 'owner/name', got {repo!r}")
    return repo


def _check_ref(ref: str) -> str:
    if not _REF.match(ref) or ".." in ref:
        raise ValueError(f"bad git ref {ref!r}")
    return ref


class GitHubClient:
    def __init__(
        self,
        token: str | None = None,
        *,
        base_url: str = DEFAULT_BASE_URL,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._transport = transport

    def __repr__(self) -> str:
        return f"GitHubClient(base_url={self._base_url!r})"

    def _headers(self, accept: str) -> dict[str, str]:
        headers = {"Accept": accept, "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "prm"}
        token = self._token or os.environ.get("GITHUB_TOKEN")
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    async def _request(
        self,
        method: str,
        path: str,
        *,
        accept: str = "application/vnd.github+json",
        json: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        allow_404: bool = False,
        follow_redirects: bool = False,
    ) -> httpx.Response | None:
        async with httpx.AsyncClient(
            base_url=self._base_url,
            transport=self._transport,
            timeout=TIMEOUT_S,
            follow_redirects=follow_redirects,
        ) as client:
            try:
                resp = await client.request(
                    method, path, headers=self._headers(accept), json=json, params=params
                )
            except httpx.HTTPError as exc:
                raise GitHubError(f"{method} {path}: {type(exc).__name__}") from None
        log.info("github %s %s -> %s", method, path, resp.status_code)
        if resp.status_code == 404 and allow_404:
            return None
        if resp.status_code >= 400:
            raise GitHubError(f"{method} {path}: HTTP {resp.status_code}")
        return resp

    async def _ok(self, method: str, path: str, **kw: Any) -> httpx.Response:
        resp = await self._request(method, path, **kw)
        if resp is None:  # only possible with allow_404, which callers of _ok never pass
            raise GitHubError(f"{method} {path}: HTTP 404")
        return resp

    # ------------------------------------------------------------------------------------------ reads

    async def get_pr(self, repo: str, number: int) -> PullRequest:
        data = (await self._ok("GET", f"/repos/{check_repo(repo)}/pulls/{int(number)}")).json()
        head_repo = (data.get("head") or {}).get("repo") or {}
        return PullRequest(
            number=int(data["number"]),
            title=data.get("title") or "",
            body=data.get("body") or "",
            head_sha=data["head"]["sha"],
            base_sha=data["base"]["sha"],
            head_repo=head_repo.get("full_name"),
            base_repo=data["base"]["repo"]["full_name"],
            draft=bool(data.get("draft", False)),
            labels=tuple(lbl["name"] for lbl in data.get("labels") or []),
        )

    async def get_diff(self, repo: str, number: int) -> str:
        path = f"/repos/{check_repo(repo)}/pulls/{int(number)}"
        return (await self._ok("GET", path, accept="application/vnd.github.diff")).text

    async def get_file(self, repo: str, path: str, ref: str) -> str | None:
        """Raw file content at ``ref`` (e.g. ``.prm.toml`` at the base sha), or None if it doesn't exist."""
        rel = PurePosixPath(path)
        if not path or rel.is_absolute() or ".." in rel.parts:
            raise ValueError(f"path must be relative inside the repo, got {path!r}")
        url = f"/repos/{check_repo(repo)}/contents/{quote(path)}"
        resp = await self._request(
            "GET", url, accept="application/vnd.github.raw", params={"ref": _check_ref(ref)}, allow_404=True
        )
        return None if resp is None else resp.text

    async def get_labels(self, repo: str, number: int) -> list[str]:
        resp = await self._ok("GET", f"/repos/{check_repo(repo)}/issues/{int(number)}/labels")
        return [lbl["name"] for lbl in resp.json()]

    async def download_tree(self, repo: str, sha: str, dest: Path) -> Path:
        """Download the tree at ``sha`` and extract it into ``dest`` (top-level dir stripped, safely)."""
        path = f"/repos/{check_repo(repo)}/tarball/{_check_ref(sha)}"
        resp = await self._ok("GET", path, follow_redirects=True)
        if len(resp.content) > MAX_TARBALL_BYTES:
            raise GitHubError(f"GET {path}: tarball larger than {MAX_TARBALL_BYTES} bytes")
        return safe_extract_tarball(resp.content, dest)

    # ------------------------------------------------------------------------------------------ writes

    async def post_status(
        self,
        repo: str,
        sha: str,
        state: StatusState,
        description: str,
        target_url: str | None = None,
        context: str = STATUS_CONTEXT,
    ) -> None:
        if state not in _STATES:
            raise ValueError(f"status state must be one of {sorted(_STATES)}, got {state!r}")
        body: dict[str, Any] = {
            "state": state,
            "description": description[:MAX_DESCRIPTION],
            "context": context,
        }
        if target_url:
            body["target_url"] = target_url
        await self._ok("POST", f"/repos/{check_repo(repo)}/statuses/{_check_ref(sha)}", json=body)

    async def merge_pr(self, repo: str, number: int, sha: str) -> bool:
        """P1: squash-merge, only if the head is still ``sha``. True if GitHub merged it."""
        path = f"/repos/{check_repo(repo)}/pulls/{int(number)}/merge"
        resp = await self._ok("PUT", path, json={"merge_method": "squash", "sha": _check_ref(sha)})
        return bool(resp.json().get("merged", False))


def safe_extract_tarball(data: bytes, dest: Path) -> Path:
    """Extract a GitHub tarball into ``dest``, stripping its top-level directory.

    Member by member, never ``extractall``: any absolute path, ``..`` component, symlink, hardlink or special
    file rejects the whole archive (fail closed), and every target must resolve inside ``dest``.
    """
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    root = dest.resolve()
    total = 0
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:*") as tar:
        members = tar.getmembers()
        for m in members:
            name = m.name
            parts = PurePosixPath(name).parts
            if name.startswith(("/", "\\")) or ".." in parts or "\\" in name:
                raise UnsafeArchive(f"unsafe path in archive: {name!r}")
            if m.issym() or m.islnk():
                raise UnsafeArchive(f"link in archive: {name!r}")
            if not (m.isfile() or m.isdir()):
                raise UnsafeArchive(f"special file in archive: {name!r}")
            total += max(m.size, 0)
            if total > MAX_EXTRACTED_BYTES:
                raise UnsafeArchive("archive expands beyond the size limit")
        for m in members:
            rel = PurePosixPath(m.name).parts[1:]  # strip "<owner>-<repo>-<sha>/"
            if not rel:
                continue
            target = root.joinpath(*rel)
            if not target.resolve().is_relative_to(root):
                raise UnsafeArchive(f"path escapes destination: {m.name!r}")
            if m.isdir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(m)
            if src is None:
                raise UnsafeArchive(f"unreadable member: {m.name!r}")
            with src, open(target, "wb") as out:
                shutil.copyfileobj(src, out)
            target.chmod(0o755 if m.mode & 0o111 else 0o644)
    return dest
