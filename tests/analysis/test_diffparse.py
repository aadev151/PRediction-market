import difflib

from hypothesis import given
from hypothesis import strategies as st

from prm.analysis.diffparse import parse_diff

GIT_DIFF = """\
diff --git a/app/main.py b/app/main.py
index 1111111..2222222 100644
--- a/app/main.py
+++ b/app/main.py
@@ -10,4 +10,4 @@ def delete_note(note_id):
     note = store.get(note_id)
-    if not is_admin(request):
-        raise HTTPException(401)
+    # admin check moved
+    pass
     return note
diff --git a/tests/test_main.py b/tests/test_main.py
new file mode 100644
index 0000000..3333333
--- /dev/null
+++ b/tests/test_main.py
@@ -0,0 +1,2 @@
+def test_ok():
+    assert True
diff --git a/old.py b/old.py
deleted file mode 100644
index 4444444..0000000
--- a/old.py
+++ /dev/null
@@ -1 +0,0 @@
-x = 1
diff --git a/a/name.py b/b/name.py
similarity index 100%
rename from a/name.py
rename to b/name.py
diff --git a/logo.png b/logo.png
index 5555555..6666666 100644
Binary files a/logo.png and b/logo.png differ
"""


def test_git_diff_files_statuses_and_paths():
    files = parse_diff(GIT_DIFF)
    assert [(f.path, f.status) for f in files] == [
        ("app/main.py", "modified"),
        ("tests/test_main.py", "added"),
        ("old.py", "deleted"),
        ("b/name.py", "renamed"),
        ("logo.png", "modified"),
    ]
    assert files[1].old_path is None and files[2].new_path is None
    assert files[3].old_path == "a/name.py"
    assert files[4].binary and not files[4].hunks


def test_line_numbers_and_kinds():
    main = parse_diff(GIT_DIFF)[0]
    (hunk,) = main.hunks
    assert (hunk.old_start, hunk.old_len, hunk.new_start, hunk.new_len) == (10, 4, 10, 4)
    assert hunk.header == "def delete_note(note_id):"
    assert [(ln.text.strip(), ln.old_no) for ln in main.removed] == [
        ("if not is_admin(request):", 11),
        ("raise HTTPException(401)", 12),
    ]
    assert [ln.new_no for ln in main.added] == [11, 12]
    context = [ln for ln in hunk.lines if ln.kind == " "]
    assert [(ln.old_no, ln.new_no) for ln in context] == [(10, 10), (13, 13)]


def test_raw_sections_reassemble_the_diff():
    files = parse_diff(GIT_DIFF)
    assert "".join(f.raw for f in files) == GIT_DIFF
    assert files[1].raw.startswith("diff --git a/tests/test_main.py")


def test_removed_line_that_looks_like_a_header_stays_content():
    diff = "--- a/q.sql\n+++ b/q.sql\n@@ -1,2 +1,1 @@\n--- a comment\n SELECT 1;\n"
    (f,) = parse_diff(diff)
    assert [ln.text for ln in f.removed] == ["-- a comment"]


def test_no_newline_marker_and_crlf():
    diff = "--- a/x\r\n+++ b/x\r\n@@ -1 +1 @@\r\n-a\r\n\\ No newline at end of file\r\n+b\r\n"
    (f,) = parse_diff(diff)
    assert [ln.text for ln in f.removed] == ["a"]
    assert [ln.text for ln in f.added] == ["b"]


def test_plain_difflib_multi_file_diff():
    a = ["x = 1\n", "y = 2\n"]
    b = ["x = 1\n", "y = 3\n"]
    diff = "".join(difflib.unified_diff(a, b, "a/one.py", "b/one.py"))
    diff += "".join(difflib.unified_diff(a, b, "a/two.py", "b/two.py"))
    files = parse_diff(diff)
    assert [f.path for f in files] == ["one.py", "two.py"]
    assert all(f.status == "modified" and len(f.added) == 1 for f in files)


def test_garbage_and_empty_input():
    assert parse_diff("") == []
    assert parse_diff("hello\nworld\n@@ nope @@\n") == []


def test_malformed_hunk_does_not_raise():
    files = parse_diff("--- a/x\n+++ b/x\n@@ -1,5 +1,5 @@\n-a\n+b\n")
    assert len(files) == 1 and len(files[0].removed) == 1


@given(st.text())
def test_never_raises(text):
    parse_diff(text)


LINES_A = st.lists(st.text(alphabet="abc \n", max_size=8), max_size=6)
LINES_B = st.lists(st.text(max_size=8), max_size=6)


@given(LINES_A, LINES_B)
def test_difflib_roundtrip_counts(a, b):
    a = [s.replace("\n", "") + "\n" for s in a]
    b = [s.replace("\n", "").replace("\r", "") + "\n" for s in b]
    diff = "".join(difflib.unified_diff(a, b, "a/f.txt", "b/f.txt"))
    files = parse_diff(diff)
    removed = sum(len(f.removed) for f in files)
    added = sum(len(f.added) for f in files)
    sm = difflib.SequenceMatcher(a=a, b=b)
    exp_removed = sum(i2 - i1 for tag, i1, i2, _, _ in sm.get_opcodes() if tag in ("delete", "replace"))
    exp_added = sum(j2 - j1 for tag, _, _, j1, j2 in sm.get_opcodes() if tag in ("insert", "replace"))
    assert (removed, added) == (exp_removed, exp_added)
