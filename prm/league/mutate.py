"""WP G. Deterministic mutants -> diff + neutral description (+ private meta). PLAN §6.10.

``generate_mutants`` applies 1-2 generic operators per mutant (benign, benign+benign, bug, or benign+bug;
``round(n * bug_fraction)`` mutants carry a bug operator) to files matching ``source_globs`` and not
``test_globs`` (README files only for ``edit_readme``). Changed Python files must still ``ast.parse``.
Each mutant gets ``out_dir/mutants/<id>/`` with ``head/`` (a full mutated copy of the target), a
target-relative git-style ``diff.patch``, a neutral ``description.txt`` and a PRIVATE ``meta.json``
(kind + operators). Only aggregate counts are logged (R-LG-6).
"""

from __future__ import annotations

import ast
import difflib
import json
import logging
import os
import random
import shutil
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Literal, cast

from prm.analysis.globs import matches
from prm.config import ContextSection
from prm.league.operators import BENIGN_OPERATORS, BUG_OPERATORS, OPERATORS, lang_of

log = logging.getLogger(__name__)

# Never copied into a mutant's head (environments and caches are rebuilt by the target's setup).
SKIP_DIRS = frozenset({".venv", "__pycache__", ".git", ".pytest_cache", "node_modules"})
_FORMAT = 1
_MAX_ATTEMPTS = 200

_TITLES = (
    "Refactor request handling",
    "Tidy validation helpers",
    "Small cleanup in {mod}",
    "Simplify {mod}",
    "Readability pass on {mod}",
    "Minor maintenance in {mod}",
    "Polish {mod}",
    "Housekeeping in {mod}",
)
_NOTES = ("Part of ongoing cleanup.", "Small, self-contained change.", "Follow-up to review comments.", "")
_FORBIDDEN = ("bug", "benign", *(op.name for op in OPERATORS))


@dataclass(frozen=True)
class Mutant:
    id: str  # "m0001", ...
    diff: str  # git-style unified diff with target-relative a/ b/ paths
    description: str  # neutral PR description
    head_dir: Path  # full mutated copy of the target
    kind: Literal["bug", "benign"]  # PRIVATE label (meta only)
    operators: tuple[str, ...]  # PRIVATE (meta only)


def _split(text: str) -> list[str]:
    lines = text.split("\r\n" if "\r\n" in text else "\n")
    return lines[:-1] if len(lines) > 1 and lines[-1] == "" else lines


def _join(lines: list[str], original: str) -> str:
    nl = "\r\n" if "\r\n" in original else "\n"
    return nl.join(lines) + (nl if original.endswith(nl) else "")


def _keepends(text: str) -> list[str]:
    parts = text.split("\n")
    return [p + "\n" for p in parts[:-1]] + ([parts[-1]] if parts[-1] else [])


def _diff(rel: str, old: str, new: str) -> str:
    out = [f"diff --git a/{rel} b/{rel}\n"]
    for line in difflib.unified_diff(_keepends(old), _keepends(new), f"a/{rel}", f"b/{rel}"):
        out.append(line if line.endswith("\n") else line + "\n\\ No newline at end of file\n")
    return "".join(out)


def _ignore(skip: Path) -> Callable[[str, list[str]], set[str]]:
    def ignore(dirpath: str, names: list[str]) -> set[str]:
        return {n for n in names if n in SKIP_DIRS or Path(dirpath, n).resolve() == skip}

    return ignore


def _source_files(target_dir: Path, ctx: ContextSection, skip: Path) -> dict[str, str]:
    """Mutable files (target-relative posix path -> text), sorted, never test files or skipped dirs."""
    out: dict[str, str] = {}
    for root, dirs, files in os.walk(target_dir):
        dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS and Path(root, d).resolve() != skip)
        for name in sorted(files):
            path = Path(root, name)
            rel = path.relative_to(target_dir).as_posix()
            lang = lang_of(rel)
            if lang is None or path.is_symlink() or matches(rel, ctx.test_globs):
                continue
            if lang != "md" and not matches(rel, ctx.source_globs):
                continue
            try:
                out[rel] = path.read_bytes().decode("utf-8")
            except (OSError, UnicodeDecodeError):
                continue
    return dict(sorted(out.items()))


def _attempt(
    sources: dict[str, str], plan: list[str], rng: random.Random
) -> tuple[dict[str, str], tuple[str, ...]] | str:
    """Apply one operator per plan slot; return (changed files, operator names) or a skip reason."""
    files: dict[str, list[str]] = {}
    used: list[str] = []
    for slot in plan:
        pool = list(BUG_OPERATORS if slot == "bug" else BENIGN_OPERATORS)
        rng.shuffle(pool)
        for op in pool:
            cands = [rel for rel in sources if op.accepts(rel)]
            rng.shuffle(cands)
            for rel in cands:
                new = op(files[rel] if rel in files else _split(sources[rel]), rng, str(lang_of(rel)))
                if new is not None:
                    files[rel] = new
                    used.append(op.name)
                    break
            else:
                continue
            break
        else:
            return "no_site"
    changed = {rel: _join(lines, sources[rel]) for rel, lines in files.items()}
    changed = {rel: text for rel, text in changed.items() if text != sources[rel]}
    if not changed:
        return "no_change"
    for rel, text in changed.items():
        if lang_of(rel) == "py":
            try:
                ast.parse(text)
            except (SyntaxError, ValueError):
                return "unparseable"
    return changed, tuple(used)


def _describe(files: list[str], rng: random.Random) -> str:
    mod = PurePosixPath(files[0]).stem or files[0]
    title = rng.choice(_TITLES).format(mod=mod)
    note = rng.choice(_NOTES)
    text = f"{title}\n\nTouches {', '.join(f'`{f}`' for f in files)}." + (f" {note}" if note else "") + "\n"
    if any(word in text.lower() for word in _FORBIDDEN):
        text = f"Refactor request handling\n\nTouches {len(files)} file(s).\n"
    return text


def _load_cached(mdir: Path, n: int, params: dict[str, Any]) -> list[Mutant] | None:
    out: list[Mutant] = []
    for idx in range(1, n + 1):
        d = mdir / f"m{idx:04d}"
        try:
            meta = json.loads((d / "meta.json").read_bytes())
            diff = (d / "diff.patch").read_bytes().decode("utf-8")
            description = (d / "description.txt").read_bytes().decode("utf-8")
        except (OSError, ValueError):
            return None
        if meta.get("params") != params or not (d / "head").is_dir():
            return None
        kind = cast(Literal["bug", "benign"], meta["kind"])
        out.append(Mutant(d.name, diff, description, d / "head", kind, tuple(meta["operators"])))
    return out


def generate_mutants(
    target_dir: Path, out_dir: Path, *, n: int, seed: int, bug_fraction: float, ctx: ContextSection
) -> list[Mutant]:
    """Generate (or load cached) ``n`` mutants of ``target_dir`` under ``out_dir/mutants/``."""
    if n < 0 or not 0.0 <= bug_fraction <= 1.0:
        raise ValueError("n must be >= 0 and bug_fraction in [0, 1]")
    target_dir, out_dir = target_dir.resolve(), out_dir.resolve()
    mdir = out_dir / "mutants"
    params = {
        "format": _FORMAT,
        "n": n,
        "seed": seed,
        "bug_fraction": bug_fraction,
        "source_globs": list(ctx.source_globs),
        "test_globs": list(ctx.test_globs),
    }
    cached = _load_cached(mdir, n, params)
    if cached is not None:
        log.info("league mutants: loaded %d from cache", len(cached))
        return cached
    if mdir.exists():
        shutil.rmtree(mdir)
    mdir.mkdir(parents=True)
    sources = _source_files(target_dir, ctx, out_dir)
    rng = random.Random(seed)  # noqa: S311 - reproducible mutant sampling, not crypto
    n_bug = round(n * bug_fraction)
    kinds = ["bug"] * n_bug + ["benign"] * (n - n_bug)
    rng.shuffle(kinds)
    skips: Counter[str] = Counter()
    seen: set[str] = set()
    out: list[Mutant] = []
    for idx, kind in enumerate(kinds, start=1):
        for _ in range(_MAX_ATTEMPTS):
            plan = (["benign"] if rng.random() < 0.5 else []) + [kind]
            result = _attempt(sources, plan, rng)
            if isinstance(result, str):
                skips[result] += 1
                continue
            changed, ops = result
            diff = "".join(_diff(rel, sources[rel], changed[rel]) for rel in sorted(changed))
            if diff in seen:
                skips["duplicate"] += 1
                continue
            seen.add(diff)
            break
        else:
            raise RuntimeError(f"no mutant after {_MAX_ATTEMPTS} attempts; skips so far: {dict(skips)}")
        d = mdir / f"m{idx:04d}"
        d.mkdir()
        shutil.copytree(target_dir, d / "head", symlinks=True, ignore=_ignore(out_dir))
        for rel, text in changed.items():
            (d / "head" / rel).write_bytes(text.encode("utf-8"))
        description = _describe(sorted(changed), rng)
        (d / "diff.patch").write_bytes(diff.encode("utf-8"))
        (d / "description.txt").write_bytes(description.encode("utf-8"))
        meta = {"id": d.name, "kind": kind, "operators": list(ops), "params": params}
        (d / "meta.json").write_text(json.dumps(meta, indent=2) + "\n", encoding="utf-8")
        out.append(Mutant(d.name, diff, description, d / "head", cast(Any, kind), ops))
    stats = {"generated": n, "bug": n_bug, "benign": n - n_bug, "skipped": dict(sorted(skips.items()))}
    (mdir / "stats.json").write_text(json.dumps(stats, indent=2) + "\n", encoding="utf-8")
    log.info("league mutants: %s", stats)
    return out
