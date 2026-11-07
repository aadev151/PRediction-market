"""WP B. Path globs from a target's ``.prm.toml`` ([context] test/sensitive/source globs).

Semantics (gitignore-like, the same on every Python version):
- ``**/`` matches zero or more directories, ``**`` matches anything, ``*`` and ``?`` never cross ``/``.
- A path also matches when one of its parent directories matches, so ``**/auth*`` covers
  ``app/auth/views.py``.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from functools import lru_cache


@lru_cache(maxsize=512)
def glob_to_regex(pattern: str) -> re.Pattern[str]:
    out: list[str] = []
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def _candidates(path: str) -> list[str]:
    parts = path.strip("/").split("/")
    return ["/".join(parts[:n]) for n in range(len(parts), 0, -1)]


def matches(path: str, globs: Iterable[str]) -> bool:
    """True if ``path`` (repo-relative, ``/``-separated) or one of its parent directories matches a glob."""
    cands = _candidates(path)
    return any(glob_to_regex(g).match(c) for g in globs for c in cands)
