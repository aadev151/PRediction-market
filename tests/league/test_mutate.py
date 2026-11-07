"""Mutant generation (PLAN §6.10): deterministic, cached, target-relative diffs, neutral descriptions."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from prm.config import ContextSection, load_target_config
from prm.league.mutate import SKIP_DIRS, generate_mutants
from prm.league.operators import OPERATORS

SAMPLE = Path(__file__).resolve().parents[2] / "examples" / "sample-target"

TINY_APP = '''"""Tiny app."""

import os


def check(x):
    if x < 0 or x > 10:
        raise ValueError("out of range")
    return x


def handle(x, user):
    if not user:
        return {"error": "no"}, 401
    value = check(x)
    return value * 2
'''


def _tiny(tmp_path: Path) -> Path:
    t = tmp_path / "tiny"
    (t / "pkg").mkdir(parents=True)
    (t / "pkg" / "app.py").write_text(TINY_APP)
    (t / "tests").mkdir()
    (t / "tests" / "test_app.py").write_text("def test_x():\n    assert 1 < 2\n")
    (t / "README.md").write_text("# tiny\n")
    (t / "__pycache__").mkdir()
    (t / "__pycache__" / "junk.pyc").write_bytes(b"x")
    return t


def _sample(tmp_path: Path) -> tuple[Path, ContextSection]:
    t = tmp_path / "target"
    shutil.copytree(SAMPLE, t, ignore=shutil.ignore_patterns(*SKIP_DIRS))
    return t, load_target_config(t / ".prm.toml").context


def _git_apply(target: Path, diff: str, work: Path) -> Path:
    copy = work / "copy"
    shutil.copytree(target, copy, ignore=shutil.ignore_patterns(*SKIP_DIRS))
    (work / "p.patch").write_text(diff)
    env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(work)}
    subprocess.run(
        ["git", "apply", "--whitespace=nowarn", str(work / "p.patch")],
        cwd=copy,
        env=env,
        check=True,
        shell=False,
        capture_output=True,
    )
    return copy


def test_tiny_target_basics(tmp_path: Path) -> None:
    t = _tiny(tmp_path)
    ms = generate_mutants(t, tmp_path / "out", n=12, seed=1, bug_fraction=0.5, ctx=ContextSection())
    assert [m.id for m in ms] == [f"m{i:04d}" for i in range(1, 13)]
    assert sum(m.kind == "bug" for m in ms) == 6
    for m in ms:
        assert 1 <= len(m.operators) <= 2
        assert "tests/" not in m.diff  # test files are never mutated
        assert (m.head_dir / "pkg" / "app.py").exists() and not (m.head_dir / "__pycache__").exists()
        d = m.head_dir.parent
        assert {p.name for p in d.iterdir()} == {"head", "diff.patch", "description.txt", "meta.json"}
    assert len({m.diff for m in ms}) == len(ms)


def test_deterministic_for_the_same_seed(tmp_path: Path) -> None:
    t = _tiny(tmp_path)
    a = generate_mutants(t, tmp_path / "a", n=10, seed=3, bug_fraction=0.4, ctx=ContextSection())
    b = generate_mutants(t, tmp_path / "b", n=10, seed=3, bug_fraction=0.4, ctx=ContextSection())
    c = generate_mutants(t, tmp_path / "c", n=10, seed=4, bug_fraction=0.4, ctx=ContextSection())
    key = [(m.diff, m.description, m.kind, m.operators) for m in a]
    assert key == [(m.diff, m.description, m.kind, m.operators) for m in b]
    assert key != [(m.diff, m.description, m.kind, m.operators) for m in c]


def test_sample_target_fraction_diffs_and_descriptions(tmp_path: Path) -> None:
    t, ctx = _sample(tmp_path)
    ms = generate_mutants(t, tmp_path / "out", n=40, seed=7, bug_fraction=0.4, ctx=ctx)
    assert len(ms) == 40
    assert 14 <= sum(m.kind == "bug" for m in ms) <= 18
    assert {m.kind for m in ms if any(op.kind == "bug" and op.name in m.operators for op in OPERATORS)} == {
        "bug"
    }
    forbidden = ["bug", "benign", *(op.name for op in OPERATORS)]
    for m in ms:
        assert not any(w in m.description.lower() for w in forbidden), m.description
        for line in m.diff.splitlines():
            if line.startswith(("--- ", "+++ ")):
                assert line[4:6] in ("a/", "b/") and not line[6:].startswith("/")
                assert str(tmp_path) not in line
    for m in ms[:8]:
        work = tmp_path / f"apply-{m.id}"
        work.mkdir()
        copy = _git_apply(t, m.diff, work)
        changed = {line[6:] for line in m.diff.splitlines() if line.startswith("+++ b/")}
        for rel in changed:
            assert (copy / rel).read_bytes() == (m.head_dir / rel).read_bytes()


def test_cache_loads_instead_of_regenerating(tmp_path: Path) -> None:
    t = _tiny(tmp_path)
    out = tmp_path / "out"
    first = generate_mutants(t, out, n=5, seed=2, bug_fraction=0.4, ctx=ContextSection())
    marker = out / "mutants" / "m0001" / "head" / "MARKER"
    marker.write_text("kept")
    again = generate_mutants(t, out, n=5, seed=2, bug_fraction=0.4, ctx=ContextSection())
    assert again == first and marker.exists()
    other = generate_mutants(t, out, n=5, seed=9, bug_fraction=0.4, ctx=ContextSection())
    assert not marker.exists() and [m.id for m in other] == [m.id for m in first]


def test_out_dir_inside_target_is_not_copied(tmp_path: Path) -> None:
    t = _tiny(tmp_path)
    ms = generate_mutants(t, t / "cache", n=2, seed=0, bug_fraction=0.0, ctx=ContextSection())
    assert all(m.kind == "benign" for m in ms)
    assert not (ms[0].head_dir / "cache").exists()


def test_bad_arguments(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        generate_mutants(tmp_path, tmp_path / "o", n=1, seed=0, bug_fraction=1.5, ctx=ContextSection())
    assert (
        generate_mutants(tmp_path, tmp_path / "o", n=0, seed=0, bug_fraction=0.4, ctx=ContextSection()) == []
    )
