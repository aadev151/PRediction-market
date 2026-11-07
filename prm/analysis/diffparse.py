"""WP B. Parse unified diffs (git-style or plain ``---``/``+++``) into files, hunks and lines. PLAN §2.

Pure and total: any text parses (garbage yields fewer files), so callers never need a try/except.
Hunk bodies are consumed by their ``@@`` line counts, so a removed line that looks like ``--- x``
stays content.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

LineKind = Literal["+", "-", " "]
FileStatus = Literal["added", "deleted", "modified", "renamed"]

_HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@ ?(.*)$")
_DIFF_GIT = re.compile(r'^diff --git "?a/(.+?)"? "?b/(.+?)"?$')


@dataclass(frozen=True)
class DiffLine:
    kind: LineKind
    text: str
    old_no: int | None  # line number in the old file (removed and context lines)
    new_no: int | None  # line number in the new file (added and context lines)


@dataclass(frozen=True)
class Hunk:
    old_start: int
    old_len: int
    new_start: int
    new_len: int
    header: str
    lines: tuple[DiffLine, ...]


@dataclass(frozen=True)
class FileDiff:
    old_path: str | None  # None for an added file
    new_path: str | None  # None for a deleted file
    status: FileStatus
    binary: bool
    hunks: tuple[Hunk, ...]
    raw: str  # this file's section of the original diff, verbatim

    @property
    def path(self) -> str:
        return self.new_path or self.old_path or ""

    @property
    def added(self) -> list[DiffLine]:
        return [ln for h in self.hunks for ln in h.lines if ln.kind == "+"]

    @property
    def removed(self) -> list[DiffLine]:
        return [ln for h in self.hunks for ln in h.lines if ln.kind == "-"]


@dataclass
class _Builder:
    old_path: str | None = None
    new_path: str | None = None
    status: FileStatus | None = None
    binary: bool = False
    hunks: list[Hunk] = field(default_factory=list)
    raw: list[str] = field(default_factory=list)
    saw_minus: bool = False
    saw_plus: bool = False

    def build(self) -> FileDiff:
        status = self.status
        if status is None:
            if self.old_path is None:
                status = "added"
            elif self.new_path is None:
                status = "deleted"
            elif self.old_path != self.new_path:
                status = "renamed"
            else:
                status = "modified"
        return FileDiff(
            self.old_path, self.new_path, status, self.binary, tuple(self.hunks), "".join(self.raw)
        )


def _clean_path(raw: str) -> str | None:
    path = raw.split("\t", 1)[0].strip().strip('"')
    if path == "/dev/null":
        return None
    if path[:2] in ("a/", "b/"):
        path = path[2:]
    return path


def _split_lines(text: str) -> list[str]:
    """Split on ``\\n`` only (``str.splitlines`` would also split on form feeds etc. inside a line)."""
    parts = text.split("\n")
    return [p + "\n" for p in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def parse_diff(text: str) -> list[FileDiff]:
    lines = _split_lines(text)
    files: list[FileDiff] = []
    cur: _Builder | None = None

    def flush() -> None:
        if cur is not None and (cur.old_path is not None or cur.new_path is not None):
            files.append(cur.build())

    i = 0
    while i < len(lines):
        line = lines[i]
        body = line.rstrip("\r\n")
        m_git = _DIFF_GIT.match(body)
        if m_git:
            flush()
            cur = _Builder(old_path=m_git.group(1), new_path=m_git.group(2))
        elif body.startswith("--- ") and (cur is None or cur.saw_minus or cur.hunks):
            flush()  # a plain (non-git) diff: every file starts at its --- line
            cur = _Builder(old_path=_clean_path(body[4:]), saw_minus=True)
        elif cur is None:
            i += 1
            continue
        elif body.startswith("--- "):
            cur.old_path, cur.saw_minus = _clean_path(body[4:]), True
        elif body.startswith("+++ ") and not cur.saw_plus:
            cur.new_path, cur.saw_plus = _clean_path(body[4:]), True
        elif body.startswith("new file mode"):
            cur.status, cur.old_path = "added", None
        elif body.startswith("deleted file mode"):
            cur.status, cur.new_path = "deleted", None
        elif body.startswith("rename from "):
            cur.status, cur.old_path = "renamed", body[len("rename from ") :]
        elif body.startswith("rename to "):
            cur.status, cur.new_path = "renamed", body[len("rename to ") :]
        elif body.startswith(("Binary files ", "GIT binary patch")):
            cur.binary = True
        elif m_hunk := _HUNK.match(body):
            i = _read_hunk(lines, i, m_hunk, cur)
            continue
        cur.raw.append(line)
        i += 1
    flush()
    return files


def _read_hunk(lines: list[str], i: int, m: re.Match[str], cur: _Builder) -> int:
    old_start, new_start = int(m.group(1)), int(m.group(3))
    old_len = int(m.group(2)) if m.group(2) is not None else 1
    new_len = int(m.group(4)) if m.group(4) is not None else 1
    cur.raw.append(lines[i])
    i += 1
    old_left, new_left = old_len, new_len
    old_no, new_no = old_start, new_start
    out: list[DiffLine] = []
    while i < len(lines) and (old_left > 0 or new_left > 0):
        body = lines[i].rstrip("\r\n")
        tag, content = (body[:1] or " "), body[1:]
        if tag == "\\":  # "\ No newline at end of file"
            pass
        elif tag == "-" and old_left > 0:
            out.append(DiffLine("-", content, old_no, None))
            old_no, old_left = old_no + 1, old_left - 1
        elif tag == "+" and new_left > 0:
            out.append(DiffLine("+", content, None, new_no))
            new_no, new_left = new_no + 1, new_left - 1
        elif tag == " " and old_left > 0 and new_left > 0:
            out.append(DiffLine(" ", content, old_no, new_no))
            old_no, new_no, old_left, new_left = old_no + 1, new_no + 1, old_left - 1, new_left - 1
        else:
            break  # malformed hunk: stop here and let the outer loop resync
        cur.raw.append(lines[i])
        i += 1
    if i < len(lines) and lines[i].startswith("\\"):
        cur.raw.append(lines[i])
        i += 1
    cur.hunks.append(Hunk(old_start, old_len, new_start, new_len, m.group(5), tuple(out)))
    return i
