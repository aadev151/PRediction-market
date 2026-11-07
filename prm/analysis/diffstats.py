"""WP B. Source vs test change stats via TargetConfig.context globs. PLAN §6.5.

A file is a *test* file if it matches ``test_globs``; otherwise a *source* file if it matches
``source_globs``; otherwise *other* (docs, config, ...). A changed source file counts as covered when some
changed test file's name contains its stem (``notes.py`` ↔ ``test_notes.py`` / ``notes.test.ts``). That is a
name heuristic, the same for every language; with no test change at all, every changed source file is
uncovered.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

from prm.analysis.diffparse import FileDiff
from prm.analysis.globs import matches
from prm.config import ContextSection


@dataclass(frozen=True)
class DiffStats:
    files_changed: int
    src_added: int
    src_removed: int
    test_added: int
    test_removed: int
    other_added: int
    other_removed: int
    source_files: tuple[str, ...]
    test_files: tuple[str, ...]
    untested_source_files: tuple[str, ...]  # changed (not deleted) source files with no matching test change

    def render(self) -> str:
        untested = ", ".join(self.untested_source_files) or "none"
        return (
            f"files changed: {self.files_changed}\n"
            f"source lines: +{self.src_added} -{self.src_removed} in {len(self.source_files)} file(s)\n"
            f"test lines: +{self.test_added} -{self.test_removed} in {len(self.test_files)} file(s)\n"
            f"other lines: +{self.other_added} -{self.other_removed}\n"
            f"changed source files with no matching test change: {untested}"
        )


def is_test_path(path: str, ctx: ContextSection) -> bool:
    return matches(path, ctx.test_globs)


def is_source_path(path: str, ctx: ContextSection) -> bool:
    return not is_test_path(path, ctx) and matches(path, ctx.source_globs)


def _stem(path: str) -> str:
    return PurePosixPath(path).name.split(".", 1)[0].lower()


def compute_stats(files: list[FileDiff], ctx: ContextSection) -> DiffStats:
    counts = {"src": [0, 0], "test": [0, 0], "other": [0, 0]}
    sources: list[FileDiff] = []
    tests: list[FileDiff] = []
    for f in files:
        if is_test_path(f.path, ctx):
            kind = "test"
            tests.append(f)
        elif is_source_path(f.path, ctx):
            kind = "src"
            sources.append(f)
        else:
            kind = "other"
        counts[kind][0] += len(f.added)
        counts[kind][1] += len(f.removed)
    test_names = [PurePosixPath(t.path).name.lower() for t in tests]
    untested = tuple(
        f.path
        for f in sources
        if f.status != "deleted" and not any(_stem(f.path) in name for name in test_names)
    )
    return DiffStats(
        files_changed=len(files),
        src_added=counts["src"][0],
        src_removed=counts["src"][1],
        test_added=counts["test"][0],
        test_removed=counts["test"][1],
        other_added=counts["other"][0],
        other_removed=counts["other"][1],
        source_files=tuple(f.path for f in sources),
        test_files=tuple(t.path for t in tests),
        untested_source_files=untested,
    )


def extract_test_diff(files: list[FileDiff], ctx: ContextSection) -> str:
    """The raw diff sections of test files only (the coverage trader's <test_diff> slice)."""
    return "".join(f.raw for f in files if is_test_path(f.path, ctx))
