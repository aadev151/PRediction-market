import pytest
from hypothesis import given
from hypothesis import strategies as st

from prm.analysis.signals import render_signals, signals_for_diff
from prm.config import ContextSection

CTX = ContextSection()
# Fake secret values are built at runtime so no key-shaped literal sits in the repo for gitleaks to flag.
FAKE_SECRET = "x" * 24


def diff(path: str, removed: list[str] = (), added: list[str] = (), context: list[str] = ()) -> str:
    body = "".join(f" {c}\n" for c in context)
    body += "".join(f"-{r}\n" for r in removed) + "".join(f"+{a}\n" for a in added)
    n_old, n_new = len(context) + len(removed), len(context) + len(added)
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1,{n_old} +1,{n_new} @@\n{body}"


def ids(d: str, ctx: ContextSection = CTX) -> list[tuple[str, str]]:
    return [(s.id, s.severity) for s in signals_for_diff(d, ctx)]


def test_removed_auth_check_and_guard():
    d = diff("app/notes.py", removed=["    if not is_admin(request):", "        raise HTTPException(401)"])
    assert ids(d) == [("removed_check", "high"), ("removed_guard", "high")]


@pytest.mark.parametrize(
    "line",
    [
        "@login_required",
        "    verify_signature(body, sig)",
        "    validate(payload)",
        "    check_permissions(user)",
        "    require_admin(user)",
        "    csrf_protect(app)",
        "    headers['Authorization']",
    ],
)
def test_removed_check_variants(line):
    assert ("removed_check", "high") in ids(diff("app/x.py", removed=[line]))


@pytest.mark.parametrize(
    "line",
    ["    raise ValueError('bad')", "    throw new Error('x')", "    return 403", "  return abort(404)"],
)
def test_removed_guard_variants(line):
    assert ("removed_guard", "high") in ids(diff("app/x.js", removed=[line]))


def test_moved_or_reindented_lines_do_not_fire():
    d = diff(
        "app/x.py",
        removed=["if not is_admin(r):", "    raise E()"],
        added=["  if not is_admin(r):", "raise E()"],
    )
    assert ids(d) == []


def test_check_still_called_after_refactor_does_not_fire():
    d = diff(
        "app/x.py", removed=["    if not verify(token):"], added=["    if not verify(token, strict=True):"]
    )
    assert "removed_check" not in [i for i, _ in ids(d)]


@pytest.mark.parametrize(
    ("line", "sig"),
    [
        ("    eval(user_input)", "dangerous_call"),
        ("    exec(code)", "dangerous_call"),
        ("    subprocess.run(cmd, shell=True)", "dangerous_call"),
        ("    os.system('rm ' + path)", "dangerous_call"),
        ("  const cp = require('child_process')", "dangerous_call"),
        ('    q = "SELECT * FROM notes WHERE id = " + note_id', "sql_string_build"),
        ('    q = "DELETE FROM t WHERE id = %s" % x', "sql_string_build"),
        ("    q = `UPDATE t SET a = ${a}`", "sql_string_build"),
        ('    q = f"SELECT * FROM notes WHERE id = {note_id}"', "sql_string_build"),
        (f'    API_KEY = "{FAKE_SECRET}"', "secret_literal"),
        ("    password: 'hunter2hunter2'", "secret_literal"),
    ],
)
def test_added_line_signals(line, sig):
    assert (sig, "high") in ids(diff("app/x.py", added=[line]))


def test_parameterized_sql_does_not_fire():
    d = diff("app/x.py", added=['    cur.execute("SELECT * FROM t WHERE id = ?", (x,))'])
    assert ids(d) == []


def test_secret_is_redacted_in_excerpt():
    (sig,) = signals_for_diff(diff("app/x.py", added=[f'TOKEN = "{FAKE_SECRET}"']), CTX)
    assert FAKE_SECRET not in sig.excerpt
    assert sig.excerpt == 'TOKEN = "***"'
    assert sig.line == 1


@pytest.mark.parametrize(("old", "new"), [("<", "<="), (">=", ">"), ("==", "!="), ("===", "!==")])
def test_comparison_flip(old, new):
    d = diff("app/x.py", removed=[f"    if n {old} limit:"], added=[f"    if n {new} limit:"])
    assert ids(d) == [("comparison_changed", "high")]


def test_comparison_with_other_edits_and_arrows_do_not_fire():
    assert ids(diff("app/x.py", removed=["if a < b:"], added=["if a < c:"])) == []
    assert ids(diff("app/x.ts", removed=["const f = (a) => a"], added=["const f = (a) >= a"])) == []
    assert ids(diff("app/x.cpp", removed=["p->x = 1"], added=["p->y = 1"])) == []


def test_sensitive_path_and_ci_change():
    assert ids(diff("app/auth/session.py", added=["x = 1"])) == [("sensitive_path", "low")]
    assert ids(diff(".github/workflows/ci.yml", added=["  run: echo hi"])) == [
        ("ci_change", "high"),
        ("sensitive_path", "low"),
    ]


def test_signals_in_test_files_are_downgraded():
    d = diff("tests/test_x.py", added=['    token = "fake-token-123456"'])
    assert ids(d) == [("secret_literal", "low")]


def test_custom_sensitive_globs():
    ctx = ContextSection(sensitive_globs=["billing/**"])
    assert ids(diff("billing/charge.go", added=["x := 1"]), ctx) == [("sensitive_path", "low")]
    assert ids(diff("app/auth.py", added=["x = 1"]), ctx) == []


def test_benign_docstring_change_is_quiet():
    d = diff("app/notes.py", added=['    """Return the note or 404."""'], context=["def get_note(i):"])
    assert ids(d) == []


def test_render_signals():
    sigs = signals_for_diff(diff("app/x.py", added=["eval(x)"] * 3), CTX)
    assert render_signals([]) == "none"
    out = render_signals(sigs, limit=2)
    assert out.splitlines()[0] == "[high] dangerous_call app/x.py:1  eval(x)"
    assert out.endswith("... 1 more")


@given(st.lists(st.text(max_size=40), max_size=5), st.lists(st.text(max_size=40), max_size=5))
def test_never_raises(removed, added):
    clean = [s.replace("\n", " ").replace("\r", " ") for s in removed]
    signals_for_diff(diff("app/x.py", clean, [s.replace("\n", " ").replace("\r", " ") for s in added]), CTX)
