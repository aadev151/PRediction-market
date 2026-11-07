"""WP B. Regex risk signals over added/removed lines -> list[prm.types.Signal]. PLAN §6.6.

Language-agnostic and pure. Noise control:
- a line that was only moved or re-indented (same text on the other side of the same file) never fires;
- ``removed_check`` fires only for a *net* removal of the check's identifier within the file;
- every signal in a test file (``test_globs``) is downgraded to ``low``;
- secret-looking values are redacted in excerpts, so a signal never repeats a secret.
"""

from __future__ import annotations

import re
from collections import Counter

from prm.analysis.diffparse import DiffLine, FileDiff, parse_diff
from prm.analysis.diffstats import is_test_path
from prm.analysis.globs import matches
from prm.config import ContextSection
from prm.types import Severity, Signal

EXCERPT_MAX = 160

_CHECK = re.compile(
    r"\w*(auth|permission|authorize|login_required|require_\w+|is_admin|verify|validate|check_\w+|csrf)\w*",
    re.IGNORECASE,
)
_GUARD = re.compile(r"\b(raise|throw)\b|\breturn\b.*(\b40\d\b|abort)")
_DANGEROUS = re.compile(r"\beval\(|\bexec\(|subprocess|child_process|os\.system|shell\s*=\s*True")
_SQL = re.compile(
    r"\b(SELECT|INSERT|UPDATE|DELETE)\b.*(\+|%|\$\{|\.format\()"
    r"|\bf[\"'][^\"']*\b(SELECT|INSERT|UPDATE|DELETE)\b[^\"']*\{"
)
_SECRET = re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"][^'\"]{8,}")
_SECRET_VALUE = re.compile(r"(['\"])[^'\"]{8,}")
_CMP = re.compile(r"(?<![-=<>!])(===|!==|==|!=|<=|>=|<|>)(?![=<>])")
_CI_PATHS = (
    ".github/workflows/**",
    ".github/actions/**",
    ".gitlab-ci.yml",
    ".circleci/**",
    "Jenkinsfile",
    "azure-pipelines.yml",
    "bitbucket-pipelines.yml",
    ".buildkite/**",
)


def _norm(text: str) -> str:
    return " ".join(text.split())


def _excerpt(text: str) -> str:
    return _SECRET_VALUE.sub(r"\1***", text.strip())[:EXCERPT_MAX]


def _comparison_flips(hunk_lines: tuple[DiffLine, ...]) -> list[DiffLine]:
    """Added lines identical to their paired removed line except for comparison operators."""
    out: list[DiffLine] = []
    i = 0
    while i < len(hunk_lines):
        removed: list[DiffLine] = []
        while i < len(hunk_lines) and hunk_lines[i].kind == "-":
            removed.append(hunk_lines[i])
            i += 1
        added: list[DiffLine] = []
        while i < len(hunk_lines) and hunk_lines[i].kind == "+":
            added.append(hunk_lines[i])
            i += 1
        for old, new in zip(removed, added, strict=False):
            if _CMP.findall(old.text) != _CMP.findall(new.text) and _CMP.sub("\0", old.text) == _CMP.sub(
                "\0", new.text
            ):
                out.append(new)
        if not removed and not added:
            i += 1
    return out


def file_signals(f: FileDiff, ctx: ContextSection) -> list[Signal]:
    sigs: list[Signal] = []
    downgrade = is_test_path(f.path, ctx)

    def add(sig_id: str, severity: Severity, line: int | None, text: str) -> None:
        sigs.append(Signal(sig_id, "low" if downgrade else severity, f.path, line, _excerpt(text)))

    if matches(f.path, _CI_PATHS):
        add("ci_change", "high", None, f"{f.status} CI/workflow file")
    if matches(f.path, ctx.sensitive_globs):
        add("sensitive_path", "low", None, f"{f.status} file under a sensitive path")

    removed, added = f.removed, f.added
    removed_norm = {_norm(ln.text) for ln in removed}
    added_norm = {_norm(ln.text) for ln in added}
    removed_ids = Counter(m.group(0).lower() for ln in removed for m in _CHECK.finditer(ln.text))
    added_ids = Counter(m.group(0).lower() for ln in added for m in _CHECK.finditer(ln.text))
    net_removed = {name for name, n in removed_ids.items() if n > added_ids[name]}

    for ln in removed:
        if _norm(ln.text) in added_norm:
            continue  # moved or re-indented, not removed
        if any(m.group(0).lower() in net_removed for m in _CHECK.finditer(ln.text)):
            add("removed_check", "high", None, ln.text)
        if _GUARD.search(ln.text):
            add("removed_guard", "high", None, ln.text)

    for ln in added:
        if _norm(ln.text) in removed_norm:
            continue
        if _DANGEROUS.search(ln.text):
            add("dangerous_call", "high", ln.new_no, ln.text)
        if _SQL.search(ln.text):
            add("sql_string_build", "high", ln.new_no, ln.text)
        if _SECRET.search(ln.text):
            add("secret_literal", "high", ln.new_no, ln.text)

    for h in f.hunks:
        for ln in _comparison_flips(h.lines):
            add("comparison_changed", "high", ln.new_no, ln.text)
    return sigs


def compute_signals(files: list[FileDiff], ctx: ContextSection) -> list[Signal]:
    return [s for f in files for s in file_signals(f, ctx)]


def signals_for_diff(diff: str, ctx: ContextSection) -> list[Signal]:
    return compute_signals(parse_diff(diff), ctx)


def render_signals(signals: list[Signal], limit: int = 50) -> str:
    if not signals:
        return "none"
    lines = [
        f"[{s.severity}] {s.id} {s.file}" + (f":{s.line}" if s.line else "") + f"  {s.excerpt}"
        for s in signals[:limit]
    ]
    if len(signals) > limit:
        lines.append(f"... {len(signals) - limit} more")
    return "\n".join(lines)
