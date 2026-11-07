"""League test helpers: a copy of the sample target and its fixture hidden tests under tmp_path."""

from __future__ import annotations

import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = ROOT / "examples"
SAMPLE = EXAMPLES / "sample-target"
SAMPLE_TESTS = EXAMPLES / "sample-target-holdouts"
BUG_PATCH = EXAMPLES / "patches" / "bug_auth.patch"
IGNORE = shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc")


def copy_sample(dest_root: Path, name: str = "sample-target") -> Path:
    """Copy the sample target to ``dest_root/name`` and its hidden tests next to it (same relative layout)."""
    target = dest_root / name
    shutil.copytree(SAMPLE, target, ignore=IGNORE)
    tests = dest_root / SAMPLE_TESTS.name
    if not tests.exists():
        shutil.copytree(SAMPLE_TESTS, tests, ignore=IGNORE)
    return target
