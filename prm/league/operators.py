"""WP G. Generic Python/JS bug and benign mutation operators. PLAN §6.10.

Each operator is ``op(lines, rng, lang) -> list[str] | None``: ``lines`` is one file without line
endings, ``lang`` is ``"py"``, ``"js"`` or ``"md"`` (READMEs), and ``None`` means "no eligible site".
Operators are regex/line based and generic (no target-specific names). String contents and comments are
masked before matching, and lines that touch a multi-line string or block comment are never edited.
Python benign operators that need scoping use ``ast``. Bug operators change behavior on purpose; benign
operators must not.
"""

from __future__ import annotations

import ast
import keyword
import random
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Literal

Lang = Literal["py", "js", "md"]
Kind = Literal["bug", "benign"]
OpFn = Callable[[list[str], random.Random, str], list[str] | None]

_JS_EXT = {".js", ".mjs", ".cjs", ".ts", ".mts", ".cts"}


def lang_of(path: str) -> Lang | None:
    """The operator language for a target-relative path, or None if no operator handles it."""
    p = PurePosixPath(path)
    if p.name.upper().startswith("README"):
        return "md"
    if p.suffix == ".py":
        return "py"
    return "js" if p.suffix in _JS_EXT else None


# ---------------------------------------------------------------- scanning


@dataclass(frozen=True)
class _Line:
    masked: str  # same length as the line: string contents -> "_", comments -> " "
    safe: bool  # starts and ends in code and touches no multi-line string / block comment
    starts_in_code: bool
    strings: tuple[tuple[int, int], ...]  # single-line '...' / "..." literals, quotes included


_MULTI = {"py": {'"""': '"""', "'''": "'''"}, "js": {"/*": "*/", "`": "`"}}


def _scan(lines: list[str], lang: str) -> list[_Line]:
    multi = _MULTI.get(lang, {})
    comment = "#" if lang == "py" else "//"
    closer: str | None = None
    out: list[_Line] = []
    for line in lines:
        starts = closer is None
        touched = not starts
        buf = list(line)
        spans: list[tuple[int, int]] = []
        i = 0
        while i < len(line):
            if closer is not None:
                if line.startswith(closer, i):
                    i += len(closer)
                    closer = None
                    continue
                step = 2 if line[i] == "\\" and closer != "*/" else 1
                for k in range(i, min(i + step, len(line))):
                    buf[k] = "_"
                i += step
                continue
            if line.startswith(comment, i):
                buf[i:] = " " * (len(line) - i)
                break
            opener = next((o for o in multi if line.startswith(o, i)), None)
            if opener is not None:
                closer, touched = multi[opener], True
                i += len(opener)
                continue
            if line[i] in "'\"":
                j = i + 1
                while j < len(line) and line[j] != line[i]:
                    j += 2 if line[j] == "\\" else 1
                end = min(j + 1, len(line))
                for k in range(i + 1, end - 1):
                    buf[k] = "_"
                spans.append((i, end))
                i = end
                continue
            i += 1
        safe = starts and not touched and closer is None
        out.append(_Line("".join(buf), safe, starts, tuple(spans)))
    return out


def _indent(line: str) -> str:
    return line[: len(line) - len(line.lstrip())]


def _balance(s: str) -> int:
    return sum(s.count(c) for c in "([{") - sum(s.count(c) for c in ")]}")


def _parse(lines: list[str]) -> ast.Module | None:
    try:
        return ast.parse("\n".join(lines) + "\n")
    except (SyntaxError, ValueError):
        return None


def _is_docstring(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.Expr)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
    )


def _unused_name(base: str, text: str) -> str:
    name, k = base, 2
    while re.search(rf"\b{re.escape(name)}\b", text):
        name, k = f"{base}_{k}", k + 1
    return name


_COND = {
    "py": re.compile(r"^\s*(if|elif|while|return)\b"),
    "js": re.compile(r"^\s*(\}\s*)?(else\s+)?(if|while|return)\b"),
}
_CMP = re.compile(r"(?<![<>=!-])(===|!==|<=|>=|==|!=|<|>)(?![<>=])")
_FLIP = {"<": "<=", ">": ">=", "<=": "<", ">=": ">", "==": "!=", "!=": "==", "===": "!==", "!==": "==="}
_INT = re.compile(r"(?<![\w.])\d+(?![\w.])")
_NUM = re.compile(r"(?<![\w.])\d+(?:\.\d+)?(?![\w.])")


def _sub_random(
    lines: list[str],
    rng: random.Random,
    lang: str,
    pattern: re.Pattern[str],
    repl: Callable[[re.Match[str]], str],
    *,
    cond_only: bool,
    group: int = 0,
) -> list[str] | None:
    if lang not in _COND:
        return None
    sites = [
        (i, m)
        for i, ln in enumerate(_scan(lines, lang))
        if ln.safe and (not cond_only or _COND[lang].match(ln.masked))
        for m in pattern.finditer(ln.masked)
    ]
    if not sites:
        return None
    i, m = rng.choice(sites)
    out = list(lines)
    out[i] = out[i][: m.start(group)] + repl(m) + out[i][m.end(group) :]
    return out


# ---------------------------------------------------------------- bug operators


def cmp_flip(lines: list[str], rng: random.Random, lang: str) -> list[str] | None:
    """``<``->``<=``, ``>``->``>=``, ``==``<->``!=`` (and back) on an if/elif/while/return line."""
    return _sub_random(lines, rng, lang, _CMP, lambda m: _FLIP[m.group()], cond_only=True)


def off_by_one(lines: list[str], rng: random.Random, lang: str) -> list[str] | None:
    """An integer literal on a condition line that has a comparison, +-1."""
    if lang not in _COND:
        return None
    sites = [
        (i, m)
        for i, ln in enumerate(_scan(lines, lang))
        if ln.safe and _COND[lang].match(ln.masked) and _CMP.search(ln.masked)
        for m in _INT.finditer(ln.masked)
    ]
    if not sites:
        return None
    i, m = rng.choice(sites)
    v = int(m.group())
    new = v + 1 if v == 0 or rng.random() < 0.5 else v - 1
    out = list(lines)
    out[i] = out[i][: m.start()] + str(new) + out[i][m.end() :]
    return out


_BOOL = {"py": re.compile(r"\b(and|or)\b"), "js": re.compile(r"&&|\|\|")}
_BOOL_SWAP = {"and": "or", "or": "and", "&&": "||", "||": "&&"}


def bool_swap(lines: list[str], rng: random.Random, lang: str) -> list[str] | None:
    if lang not in _BOOL:
        return None
    return _sub_random(lines, rng, lang, _BOOL[lang], lambda m: _BOOL_SWAP[m.group()], cond_only=False)


_NOT = {"py": re.compile(r"\bnot\b\s*"), "js": re.compile(r"(?<![\w)\]!])!(?!=)")}


def drop_not(lines: list[str], rng: random.Random, lang: str) -> list[str] | None:
    if lang not in _NOT:
        return None
    return _sub_random(lines, rng, lang, _NOT[lang], lambda m: "", cond_only=True)


def _stmt_end(scan: list[_Line], i: int) -> int:
    """Last line of the statement starting at line ``i`` (bracket/backslash continuation), or -1."""
    depth, j = _balance(scan[i].masked), i
    while depth > 0 or scan[j].masked.rstrip().endswith("\\"):
        j += 1
        if j >= len(scan) or not scan[j].safe:
            return -1
        depth += _balance(scan[j].masked)
    return j if depth == 0 else -1


def _delete_stmt(lines: list[str], scan: list[_Line], i: int, j: int, lang: str) -> list[str]:
    """Delete lines i..j, keeping the block valid: drop a now-empty one-line ``if``, else leave ``pass``."""

    def blank(k: int) -> bool:
        return not scan[k].masked.strip()

    p = i - 1
    while p >= 0 and blank(p):
        p -= 1
    q = j + 1
    while q < len(lines) and blank(q):
        q += 1
    nxt = scan[q].masked if q < len(lines) else ""
    out = list(lines)
    if lang == "py":
        ind = len(_indent(lines[i]))
        p_ind = len(_indent(lines[p])) if p >= 0 else 0
        q_ind = len(_indent(lines[q])) if q < len(lines) else 0
        opens = p >= 0 and scan[p].masked.rstrip().endswith(":") and p_ind < ind
        if not opens or (q < len(lines) and q_ind >= ind):
            del out[i : j + 1]
            return out
        head = scan[p].masked
        else_follows = q < len(lines) and q_ind == p_ind and re.match(r"\s*(elif|else)\b", nxt)
        if re.match(r"\s*if\b", head) and _balance(head) == 0 and not else_follows:
            del out[p : j + 1]
        else:
            out[i : j + 1] = [_indent(lines[i]) + "pass"]
        return out
    if (
        p >= 0
        and q < len(lines)
        and re.match(r"\s*if\s*\(.*\)\s*\{\s*$", scan[p].masked)
        and nxt.strip() == "}"
    ):
        del out[p : q + 1]
    else:
        del out[i : j + 1]
    return out


def _delete_random(
    lines: list[str], rng: random.Random, lang: str, pick: Callable[[list[_Line], int], bool]
) -> list[str] | None:
    if lang not in _COND:
        return None
    scan = _scan(lines, lang)
    sites = []
    for i, ln in enumerate(scan):
        if ln.safe and ln.masked.strip() and pick(scan, i):
            j = _stmt_end(scan, i)
            if j >= 0:
                sites.append((i, j))
    if not sites:
        return None
    i, j = rng.choice(sites)
    return _delete_stmt(lines, scan, i, j, lang)


_GUARD = {
    "py": re.compile(r"^\s*(raise\b|return\b.*\b(4\d\d|abort)\b)"),
    "js": re.compile(r"^\s*(throw\b|return\b.*\b(4\d\d|abort)\b)"),
}


def delete_guard(lines: list[str], rng: random.Random, lang: str) -> list[str] | None:
    """Delete a ``raise``/``throw``/4xx ``return`` (and its single-line ``if`` when that empties it)."""
    return _delete_random(lines, rng, lang, lambda scan, i: bool(_GUARD[lang].match(scan[i].masked)))


_CALL = re.compile(r"^\s*(?:await\s+)?([A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*)\s*\(")
_CHECKISH = re.compile(r"(?i)check|validat|verif|require|authoriz|auth")


def _is_check_call(scan: list[_Line], i: int) -> bool:
    m = _CALL.match(scan[i].masked)
    if not m:
        return False
    name = m.group(1).split(".")[-1]
    if keyword.iskeyword(name) or name == "require" or not _CHECKISH.search(name):
        return False
    j = _stmt_end(scan, i)
    if j < 0:
        return False
    text = "\n".join(ln.masked for ln in scan[i : j + 1])
    depth, k = 0, m.end() - 1
    while k < len(text):
        depth += {"(": 1, ")": -1}.get(text[k], 0)
        if depth == 0:
            break
        k += 1
    return text[k + 1 :].strip() in ("", ";")


def delete_check_call(lines: list[str], rng: random.Random, lang: str) -> list[str] | None:
    """Delete a statement that is only a call to a check/validate/verify/require/auth* function."""
    return _delete_random(lines, rng, lang, _is_check_call)


_STATUS = re.compile(
    r"(\bstatus_code\s*[=:]\s*|\bstatus\(\s*|\bsendStatus\(\s*|\bHTTPException\(\s*|\babort\(\s*)([1-5]\d\d)\b"
)
_STATUS_MAP = {
    200: 201,
    201: 200,
    202: 200,
    204: 200,
    400: 422,
    401: 403,
    403: 401,
    404: 200,
    409: 400,
    422: 400,
}


def status_change(lines: list[str], rng: random.Random, lang: str) -> list[str] | None:
    def repl(m: re.Match[str]) -> str:
        code = int(m.group(2))
        return str(_STATUS_MAP.get(code, 200 if code != 200 else 201))

    return _sub_random(lines, rng, lang, _STATUS, repl, cond_only=False, group=2)


_AUTHISH = r"(?i:auth|admin|login|user|permission|token|guard|secur|session|role|scope|require|verify)"
_DEP = re.compile(r"\bDepends\(\s*[\w.]*" + _AUTHISH)
_AUTH_DECO = re.compile(r"^\s*@[\w.]*" + _AUTHISH)


def _is_auth_dep(scan: list[_Line], i: int) -> bool:
    m = scan[i].masked
    if re.match(r"\s*(async\s+)?(def|class|function)\b", m) or _balance(m) != 0 or m.rstrip().endswith("\\"):
        return False
    return bool(_DEP.search(m) or _AUTH_DECO.match(m))


def drop_decorator_dep(lines: list[str], rng: random.Random, lang: str) -> list[str] | None:
    """Delete a balanced line holding an auth-ish ``Depends(...)`` or an auth-ish decorator."""
    return _delete_random(lines, rng, lang, _is_auth_dep)


# ---------------------------------------------------------------- benign operators

_COMMENTS = (
    "Kept explicit for readability.",
    "See the surrounding function for context.",
    "Grouped with the related logic below.",
    "Intentionally simple.",
    "Order kept as in the original module.",
    "Small step; see the caller.",
)


def add_comment(lines: list[str], rng: random.Random, lang: str) -> list[str] | None:
    if lang not in _COND:
        return None
    scan = _scan(lines, lang)
    mark = "#" if lang == "py" else "//"
    start = next((k + 1 for k in (1, 0) if k < len(lines) and re.match(r"#!|#.*coding[:=]", lines[k])), 0)
    sites = [
        i
        for i in range(start, len(lines))
        if scan[i].starts_in_code
        and lines[i].strip()
        and not (i and scan[i - 1].masked.rstrip().endswith("\\"))
    ]
    if not sites:
        return None
    i = rng.choice(sites)
    return [*lines[:i], f"{_indent(lines[i])}{mark} {rng.choice(_COMMENTS)}", *lines[i:]]


def _plain_functions(tree: ast.Module) -> list[ast.FunctionDef | ast.AsyncFunctionDef]:
    """Undecorated, non-dunder functions (decorated ones, e.g. routes, may publish their docstring)."""
    return [
        n
        for n in ast.walk(tree)
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
        and not n.decorator_list
        and not (n.name.startswith("__") and n.name.endswith("__"))
    ]


def _humanize(name: str) -> str:
    text = " ".join(w for w in name.strip("_").split("_") if w) or "helper"
    return text[0].upper() + text[1:] + "."


def add_docstring(lines: list[str], rng: random.Random, lang: str) -> list[str] | None:
    tree = _parse(lines) if lang == "py" else None
    if tree is None:
        return None
    scan = _scan(lines, lang)
    sites = []
    for f in _plain_functions(tree):
        first = f.body[0]
        if _is_docstring(first) or getattr(first, "decorator_list", None):
            continue
        row = first.lineno - 1
        if row > f.lineno - 1 and scan[row].starts_in_code and not lines[row][: first.col_offset].strip():
            sites.append((row, f.name))
    if not sites:
        return None
    row, name = rng.choice(sites)
    return [*lines[:row], f'{_indent(lines[row])}"""{_humanize(name)}"""', *lines[row:]]


def delete_docstring(lines: list[str], rng: random.Random, lang: str) -> list[str] | None:
    """Delete a one-line docstring of a plain function that has other statements (a benign deletion)."""
    tree = _parse(lines) if lang == "py" else None
    if tree is None:
        return None
    sites = []
    for f in _plain_functions(tree):
        doc = f.body[0]
        if len(f.body) > 1 and _is_docstring(doc) and doc.lineno == doc.end_lineno and doc.lineno > f.lineno:
            row = doc.lineno - 1
            if not lines[row][: doc.col_offset].strip() and f.body[1].lineno > doc.lineno:
                sites.append(row)
    if not sites:
        return None
    row = rng.choice(sites)
    return lines[:row] + lines[row + 1 :]


_SCOPES = (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef)
_COMPS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


def _direct_stores(f: ast.AST) -> set[str]:
    out: set[str] = set()
    stack = list(ast.iter_child_nodes(f))
    while stack:
        n = stack.pop()
        if isinstance(n, ast.Name) and isinstance(n.ctx, ast.Store):
            out.add(n.id)
        if not isinstance(n, _SCOPES + _COMPS):
            stack.extend(ast.iter_child_nodes(n))
    return out


def _blocked_names(f: ast.AST) -> set[str] | None:
    """Names a rename must not touch (params, global/nonlocal, defs, imports); None if ``f`` is unsafe."""
    out: set[str] = set()
    for n in ast.walk(f):
        if isinstance(n, (ast.ClassDef, ast.Match)):
            return None
        if isinstance(n, ast.arg):
            out.add(n.arg)
        elif isinstance(n, (ast.Global, ast.Nonlocal)):
            out.update(n.names)
        elif isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out.add(n.name)
        elif isinstance(n, ast.alias):
            out.add((n.asname or n.name).split(".")[0])
        elif (
            isinstance(n, ast.Call)
            and isinstance(n.func, ast.Name)
            and n.func.id in ("locals", "vars", "eval")
        ):
            return None
    return out


def rename_local(lines: list[str], rng: random.Random, lang: str) -> list[str] | None:
    """Rename one function-local variable everywhere in its function (Python, ast-located and verified)."""
    tree = _parse(lines) if lang == "py" else None
    if tree is None:
        return None
    src = "\n".join(lines)
    sites = []
    for f in ast.walk(tree):
        if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef)):
            blocked = _blocked_names(f)
            if blocked is not None:
                sites += [(f, n) for n in sorted(_direct_stores(f) - blocked) if not n.startswith("__")]
    if not sites:
        return None
    f, old = rng.choice(sites)
    new = _unused_name(f"{old}{rng.choice(('_value', '_val', '_ref'))}", src)
    nodes = [n for n in ast.walk(f) if isinstance(n, ast.Name) and n.id == old]
    out = list(lines)
    for n in sorted(nodes, key=lambda n: (n.lineno, n.col_offset), reverse=True):
        raw = out[n.lineno - 1].encode()
        out[n.lineno - 1] = (raw[: n.col_offset] + new.encode() + raw[n.end_col_offset or 0 :]).decode()
    new_tree = _parse(out)
    for n in nodes:
        n.id = new
    if new_tree is None or ast.dump(new_tree) != ast.dump(tree):
        return None
    return out


def _py_imports(tree: ast.Module) -> dict[int, ast.Import | ast.ImportFrom]:
    """Top-level single-line imports (not ``__future__``) that are alone on their line, by 0-based row."""
    rows: dict[int, int] = {}
    for n in tree.body:
        for r in range(n.lineno, (n.end_lineno or n.lineno) + 1):
            rows[r] = rows.get(r, 0) + 1
    return {
        n.lineno - 1: n
        for n in tree.body
        if isinstance(n, (ast.Import, ast.ImportFrom))
        and n.lineno == n.end_lineno
        and rows[n.lineno] == 1
        and not (isinstance(n, ast.ImportFrom) and n.module == "__future__")
        and not any(a.name == "*" for a in n.names)
    }


def _bound(a: ast.alias) -> str:
    return a.asname or a.name.split(".")[0]


_JS_IMPORT = re.compile(r"^import\s+(.+?)\s+from\s+(['\"])[^'\"]+\2;?\s*$")


def _js_import_names(line: str) -> set[str] | None:
    m = _JS_IMPORT.match(line)
    return None if m is None else set(re.findall(r"[A-Za-z_$][\w$]*", m.group(1))) - {"as", "type"}


def _import_rows(lines: list[str], lang: str) -> dict[int, set[str]]:
    scan = _scan(lines, lang)
    if lang == "py":
        tree = _parse(lines)
        if tree is None:
            return {}
        return {r: {_bound(a) for a in n.names} for r, n in _py_imports(tree).items() if scan[r].safe}
    rows = {i: _js_import_names(line) for i, line in enumerate(lines) if scan[i].safe}
    return {i: names for i, names in rows.items() if names}


def reorder_imports(lines: list[str], rng: random.Random, lang: str) -> list[str] | None:
    """Swap two adjacent top-level single-line imports that bind disjoint names."""
    if lang not in _COND:
        return None
    rows = _import_rows(lines, lang)
    sites = [r for r in rows if r + 1 in rows and not rows[r] & rows[r + 1] and lines[r] != lines[r + 1]]
    if not sites:
        return None
    r = rng.choice(sites)
    out = list(lines)
    out[r], out[r + 1] = out[r + 1], out[r]
    return out


def delete_unused_import(lines: list[str], rng: random.Random, lang: str) -> list[str] | None:
    """Drop an imported name that appears nowhere else in the file (raw text, so strings count as uses)."""
    if lang not in _COND:
        return None
    tree = _parse(lines) if lang == "py" else None
    sites: list[tuple[int, str | None]] = []
    for r, names in _import_rows(lines, lang).items():
        if "noqa" in lines[r]:
            continue
        rest = "\n".join(lines[:r] + lines[r + 1 :])
        unused = sorted(n for n in names if not re.search(rf"(?<![\w$]){re.escape(n)}(?![\w$])", rest))
        if lang == "js":
            sites += [(r, None)] if len(unused) == len(names) else []
        else:
            sites += [(r, n) for n in unused]
    if not sites:
        return None
    r, name = rng.choice(sites)
    out = list(lines)
    if tree is None or name is None:
        del out[r]
        return out
    node = _py_imports(tree)[r]
    keep = [a for a in node.names if _bound(a) != name]
    if not keep:
        del out[r]
    else:
        node.names = keep
        out[r] = _indent(lines[r]) + ast.unparse(node)
    return out


def _js_preamble_end(lines: list[str]) -> int:
    k = 0
    while k < len(lines):
        s = lines[k].strip()
        if s and not (
            s.startswith("//")
            or re.match(r"""^['"]use strict['"];?$""", s)
            or _JS_IMPORT.match(s)
            or re.match(r"^(const|let|var)\s+.+=\s*require\(.+\);?$", s)
        ):
            break
        k += 1
    return k


def extract_constant(lines: list[str], rng: random.Random, lang: str) -> list[str] | None:
    """Move one numeric literal into a module-level constant defined right after the imports."""
    if lang == "py":
        tree = _parse(lines)
        if tree is None:
            return None
        at = 0
        for k, n in enumerate(tree.body):
            if isinstance(n, (ast.Import, ast.ImportFrom)) or (k == 0 and _is_docstring(n)):
                at = n.end_lineno or n.lineno
            else:
                break
    elif lang == "js":
        at = _js_preamble_end(lines)
    else:
        return None
    scan = _scan(lines, lang)
    skip = re.compile(r"^\s*(case|global|nonlocal|import|export\s+(type|interface)|type|interface|enum)\b")
    sites = [
        (i, m)
        for i in range(at, len(lines))
        if scan[i].safe and not skip.match(scan[i].masked)
        for m in _NUM.finditer(scan[i].masked)
        if lang == "py" or not re.search(r"[:|&<]\s*$", scan[i].masked[: m.start()])
    ]
    if not sites:
        return None
    i, m = rng.choice(sites)
    idents = [w for w in re.findall(r"[A-Za-z_]\w*", scan[i].masked[: m.start()]) if not keyword.iskeyword(w)]
    stem = idents[-1].strip("_").upper() if idents and idents[-1].strip("_") else "VALUE"
    name = _unused_name(("_DEFAULT_" if lang == "py" else "DEFAULT_") + stem, "\n".join(lines))
    decl = f"{name} = {m.group()}" if lang == "py" else f"const {name} = {m.group()};"
    out = list(lines)
    out[i] = out[i][: m.start()] + name + out[i][m.end() :]
    return [*out[:at], "", decl, *out[at:]]


_README_LINES = (
    "Contributions are welcome; please open an issue first.",
    "Run the test suite before opening a pull request.",
    "See the docs folder for more details.",
    "Feedback on this README is welcome.",
)


def edit_readme(lines: list[str], rng: random.Random, lang: str) -> list[str] | None:
    """Append a neutral sentence to a README."""
    choices = [t for t in _README_LINES if t not in lines]
    if lang != "md" or not choices:
        return None
    sep = [""] if lines and lines[-1].strip() else []
    return [*lines, *sep, rng.choice(choices)]


def reformat_line(lines: list[str], rng: random.Random, lang: str) -> list[str] | None:
    """Swap a simple string literal's quote style, or parenthesize a single-line ``return`` value."""
    if lang not in _COND:
        return None
    scan = _scan(lines, lang)
    sites: list[tuple[int, int, int]] = []
    for i, ln in enumerate(scan):
        if not ln.safe:
            continue
        for s, e in ln.strings:
            body = lines[i][s + 1 : e - 1]
            if e - s >= 2 and lines[i][e - 1] == lines[i][s] and not re.search(r"['\"\\]", body):
                sites.append((i, s, e))
        m = re.match(r"^\s*return\s+(?=\S)", ln.masked)
        code = ln.masked.rstrip()
        complete = code.endswith(";") if lang == "js" else not re.search(r"[\\,{(\[]$", code.rstrip(";"))
        if m and _balance(code) == 0 and complete and code[m.end() :].strip(" ;"):
            sites.append((i, m.end(), -1))
    if not sites:
        return None
    i, s, e = rng.choice(sites)
    line = lines[i]
    out = list(lines)
    if e >= 0:
        q = '"' if line[s] == "'" else "'"
        out[i] = line[:s] + q + line[s + 1 : e - 1] + q + line[e:]
    else:
        code = scan[i].masked.rstrip()
        end = len(code) - 1 if code.endswith(";") else len(code)
        out[i] = line[:s] + "(" + line[s:end] + ")" + line[end:]
    return out


# ---------------------------------------------------------------- registry


@dataclass(frozen=True)
class Operator:
    name: str
    kind: Kind
    langs: frozenset[str]
    fn: OpFn
    skip_names: frozenset[str] = frozenset()

    def accepts(self, path: str) -> bool:
        return lang_of(path) in self.langs and PurePosixPath(path).name not in self.skip_names

    def __call__(self, lines: list[str], rng: random.Random, lang: str) -> list[str] | None:
        out = self.fn(lines, rng, lang)
        return None if out is None or out == lines else out


_CODE = frozenset({"py", "js"})
_PY = frozenset({"py"})
_REEXPORTS = frozenset({"__init__.py", "index.js", "index.ts", "index.mjs", "index.cjs"})

OPERATORS: tuple[Operator, ...] = (
    Operator("cmp_flip", "bug", _CODE, cmp_flip),
    Operator("off_by_one", "bug", _CODE, off_by_one),
    Operator("bool_swap", "bug", _CODE, bool_swap),
    Operator("drop_not", "bug", _CODE, drop_not),
    Operator("delete_guard", "bug", _CODE, delete_guard),
    Operator("delete_check_call", "bug", _CODE, delete_check_call),
    Operator("status_change", "bug", _CODE, status_change),
    Operator("drop_decorator_dep", "bug", _CODE, drop_decorator_dep),
    Operator("rename_local", "benign", _PY, rename_local),
    Operator("add_comment", "benign", _CODE, add_comment),
    Operator("add_docstring", "benign", _PY, add_docstring),
    Operator("delete_docstring", "benign", _PY, delete_docstring),
    Operator("reorder_imports", "benign", _CODE, reorder_imports),
    Operator("reformat_line", "benign", _CODE, reformat_line),
    Operator("delete_unused_import", "benign", _CODE, delete_unused_import, _REEXPORTS),
    Operator("extract_constant", "benign", _CODE, extract_constant),
    Operator("edit_readme", "benign", frozenset({"md"}), edit_readme),
)
BUG_OPERATORS = tuple(op for op in OPERATORS if op.kind == "bug")
BENIGN_OPERATORS = tuple(op for op in OPERATORS if op.kind == "benign")
BY_NAME = {op.name: op for op in OPERATORS}
