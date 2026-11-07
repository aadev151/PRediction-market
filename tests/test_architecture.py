"""Architecture invariants (R-TGT-4): the factory has no target-specific code."""

import ast
from pathlib import Path

PRM = Path(__file__).resolve().parents[1] / "prm"
FORBIDDEN_TEXT = ("examples/", "examples.", "sample-target", "sample_target")


def _files():
    return [p for p in PRM.rglob("*") if p.is_file() and "__pycache__" not in p.parts]


def test_prm_never_imports_examples():
    for path in PRM.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                mods = [a.name for a in node.names]
            elif isinstance(node, ast.ImportFrom):
                mods = [node.module or ""]
            else:
                continue
            assert not any(m == "examples" or m.startswith("examples.") for m in mods), path


def test_prm_never_references_examples_or_sample_target():
    for path in _files():
        text = path.read_text(encoding="utf-8", errors="ignore")
        hits = [s for s in FORBIDDEN_TEXT if s in text]
        assert not hits, f"{path.relative_to(PRM.parent)} references {hits}"
