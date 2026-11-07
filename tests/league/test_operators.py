"""Generic mutation operators (PLAN §6.10): each changes what it should, None when nothing is eligible."""

from __future__ import annotations

import ast
import random

import pytest

from prm.league.operators import BENIGN_OPERATORS, BY_NAME, OPERATORS, lang_of

PY = '''"""Module doc: if a < b and not c, return 404."""

import os
import sys

LIMIT = 10


def guard(x, items):
    if x < 1 or x > 100:
        raise ValueError("bad x")
    if not items:
        return {"error": "empty"}, 404
    check_items(items)
    total = 0
    for item in items:
        total = total + item
    return total


def helper(name):
    """Say hi."""
    msg = 'hello ' + name
    return msg


def compute(a, b):
    result = a * 3 + b
    return result
'''

JS = """import { readFile } from 'fs';
import path from 'path';

function guard(req, res) {
  if (!req.user && req.count >= 3) {
    throw new Error('nope');
  }
  validateRequest(req);
  return res.status(403).send('no');
}
"""

OPS = sorted(op.name for op in OPERATORS)


def _rng(seed: int) -> random.Random:
    return random.Random(seed)  # noqa: S311 - reproducible test sampling


def _run(name: str, src: str, lang: str = "py", seed: int = 0) -> list[str] | None:
    return BY_NAME[name](src.split("\n"), _rng(seed), lang)


def _changed(before: str, after: list[str]) -> tuple[list[str], list[str]]:
    a, b = before.split("\n"), after
    return [x for x in a if x not in b], [x for x in b if x not in a]


def _ns(src: str) -> dict[str, object]:
    ns: dict[str, object] = {"check_items": lambda items: None}
    exec(compile(src, "<mutant>", "exec"), ns)  # noqa: S102 - our own inline snippet
    return ns


def test_lang_of() -> None:
    assert lang_of("a/b.py") == "py"
    assert lang_of("src/x.ts") == "js"
    assert lang_of("README.md") == "md"
    assert lang_of("x.tsx") is None and lang_of("data.json") is None


@pytest.mark.parametrize("name", OPS)
def test_python_ops_keep_parsing(name: str) -> None:
    for seed in range(15):
        out = _run(name, PY, seed=seed)
        if out is not None:
            ast.parse("\n".join(out))
            assert out != PY.split("\n")


@pytest.mark.parametrize("name", OPS)
def test_none_when_no_eligible_site(name: str) -> None:
    if name == "edit_readme":
        assert _run(name, "x = 1", "py") is None
        return
    assert BY_NAME[name]([], _rng(0), "py") is None
    assert _run(name, "# just a comment\n", "md") is None


@pytest.mark.parametrize("name", [n for n in OPS if n not in ("edit_readme", "add_comment")])
def test_nothing_eligible_in_a_trivial_module(name: str) -> None:
    assert _run(name, "pass") is None


def test_cmp_flip_only_on_condition_lines_and_never_in_docstrings() -> None:
    for seed in range(20):
        removed, added = _changed(PY, _run("cmp_flip", PY, seed=seed) or [])
        assert removed == ["    if x < 1 or x > 100:"]
        assert added[0] in ("    if x <= 1 or x > 100:", "    if x < 1 or x >= 100:")


def test_off_by_one_and_bool_swap_and_drop_not() -> None:
    _, added = _changed(PY, _run("off_by_one", PY) or [])
    assert added[0] in {f"    if x < {lo} or x > {hi}:" for lo, hi in ((0, 100), (2, 100), (1, 99), (1, 101))}
    assert _changed(PY, _run("bool_swap", PY) or [])[1] == ["    if x < 1 and x > 100:"]
    assert _changed(PY, _run("drop_not", PY) or [])[1] == ["    if items:"]
    assert _run("bool_swap", JS, "js") is not None
    out = _run("drop_not", JS, "js") or []
    assert "  if (req.user && req.count >= 3) {" in out


def test_delete_guard_drops_the_if_and_keeps_python_valid() -> None:
    seen = set()
    for seed in range(20):
        out = _run("delete_guard", PY, seed=seed) or []
        ast.parse("\n".join(out))
        removed, _ = _changed(PY, out)
        seen.add(tuple(removed))
    assert ("    if x < 1 or x > 100:", '        raise ValueError("bad x")') in seen
    js = _run("delete_guard", JS, "js") or []
    assert "throw" not in "\n".join(js) or "403" not in "\n".join(js)


def test_delete_guard_leaves_pass_when_if_has_else() -> None:
    src = "def f(x):\n    if x:\n        raise ValueError()\n    else:\n        return 1\n"
    out = _run("delete_guard", src) or []
    assert "        pass" in out
    ast.parse("\n".join(out))


def test_delete_check_call_and_status_change_and_drop_dep() -> None:
    assert _changed(PY, _run("delete_check_call", PY) or [])[0] == ["    check_items(items)"]
    assert "  validateRequest(req);" not in (_run("delete_check_call", JS, "js") or [])
    assert _run("delete_check_call", "x = check(1)\n") is None
    src = "def f(\n    a: int,\n    _u: Annotated[None, Depends(require_user)],\n) -> int:\n    return a\n"
    out = _run("drop_decorator_dep", src) or []
    assert "Depends" not in "\n".join(out)
    ast.parse("\n".join(out))
    assert _run("drop_decorator_dep", "def f(db=Depends(get_db)):\n    return db\n") is None
    out = _run("status_change", "@app.post('/x', status_code=201)\ndef f():\n    return 1\n") or []
    assert out[0] == "@app.post('/x', status_code=200)"
    assert (_run("status_change", JS, "js") or [])[8] == "  return res.status(401).send('no');"


def test_benign_ops_preserve_behavior() -> None:
    base = _ns(PY)
    for op in BENIGN_OPERATORS:
        if op.name == "edit_readme":
            continue
        for seed in range(10):
            out = op(PY.split("\n"), _rng(seed), "py")
            if out is None:
                continue
            ns = _ns("\n".join(out))
            assert ns["guard"](5, [1, 2]) == base["guard"](5, [1, 2]) == 3, op.name  # type: ignore[operator]
            assert ns["guard"](5, []) == base["guard"](5, []), op.name  # type: ignore[operator]
            assert ns["helper"]("a") == "hello a", op.name  # type: ignore[operator]
            assert ns["compute"](2, 1) == 7, op.name  # type: ignore[operator]
            with pytest.raises(ValueError):
                ns["guard"](0, [1])  # type: ignore[operator]


def test_rename_local_renames_every_use_in_scope_only() -> None:
    out = _run("rename_local", PY, seed=1) or []
    removed, added = _changed(PY, out)
    assert removed and len(removed) == len(added)
    src = "def f(x):\n    y = x\n    return [y for y in range(x)] + [y]\n"
    for seed in range(5):
        ns: dict[str, object] = {}
        exec("\n".join(_run("rename_local", src, seed=seed) or []), ns)  # noqa: S102
        assert ns["f"](2) == [0, 1, 2]  # type: ignore[operator]
    assert _run("rename_local", "def f(x):\n    return locals()\n") is None


def test_delete_unused_import_only_when_unused() -> None:
    out = _run("delete_unused_import", PY) or []
    assert "import os" not in out or "import sys" not in out
    assert _run("delete_unused_import", "import os\n\nprint(os.sep)\n") is None
    assert _run("delete_unused_import", "import os  # noqa: F401\n") is None
    assert _run("delete_unused_import", "from a import b, c\n\nc()\n") == ["from a import c", "", "c()", ""]
    assert _run("delete_unused_import", "__all__ = ['b']\nfrom a import b\n") is None
    assert not BY_NAME["delete_unused_import"].accepts("pkg/__init__.py")
    out = _run("delete_unused_import", JS, "js") or []
    assert len(out) == len(JS.split("\n")) - 1 and sum(line.startswith("import ") for line in out) == 1
    assert _run("delete_unused_import", "import path from 'path';\nconsole.log(path.sep);\n", "js") is None


def test_reorder_imports_add_comment_docstring_extract_constant() -> None:
    out = _run("reorder_imports", PY) or []
    assert out.index("import sys") < out.index("import os")
    assert _run("reorder_imports", JS, "js") is not None
    out = _run("add_comment", JS, "js") or []
    assert any(line.strip().startswith("// ") for line in out)
    out = _run("add_docstring", PY) or []
    assert any('"""' in line and ("Guard." in line or "Compute." in line) for line in out)
    out = _run("extract_constant", PY) or []
    assert any(line.startswith("_DEFAULT_") for line in out)
    out = _run("extract_constant", JS, "js") or []
    assert any(line.startswith("const DEFAULT_") for line in out)
    assert out.index(next(x for x in out if x.startswith("const DEFAULT_"))) > 1


def test_reformat_line_and_edit_readme() -> None:
    def norm(s: str) -> str:
        return s.replace("'", '"').replace("(", "").replace(")", "")

    for seed in range(10):
        removed, added = _changed(PY, _run("reformat_line", PY, seed=seed) or [])
        assert len(removed) == len(added) == 1 and removed != added
        assert norm(removed[0]) == norm(added[0])
    out = _run("edit_readme", "# Title\n\nSome text.", "md") or []
    assert out[:3] == ["# Title", "", "Some text."] and len(out) == 5
    assert _run("edit_readme", "# Title", "py") is None


def test_operators_are_target_agnostic_and_classified() -> None:
    assert {op.kind for op in OPERATORS} == {"bug", "benign"}
    assert len({op.name for op in OPERATORS}) == len(OPERATORS)
    for op in OPERATORS:
        assert op.langs <= {"py", "js", "md"}
