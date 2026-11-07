from prm.analysis.diffparse import parse_diff
from prm.analysis.diffstats import compute_stats, extract_test_diff
from prm.config import ContextSection

CTX = ContextSection()


def _file(path: str, added: int, removed: int = 0, *, new: bool = False, deleted: bool = False) -> str:
    old = "/dev/null" if new else f"a/{path}"
    new_p = "/dev/null" if deleted else f"b/{path}"
    body = "".join(f"-old{i}\n" for i in range(removed)) + "".join(f"+new{i}\n" for i in range(added))
    return f"diff --git a/{path} b/{path}\n--- {old}\n+++ {new_p}\n@@ -1,{removed} +1,{added} @@\n{body}"


def test_counts_by_kind():
    diff = (
        _file("app/notes.py", 5, 2)
        + _file("tests/test_notes.py", 3)
        + _file("README.md", 1, 1)
        + _file("web/api.ts", 2)
    )
    s = compute_stats(parse_diff(diff), CTX)
    assert (s.src_added, s.src_removed, s.test_added, s.test_removed) == (7, 2, 3, 0)
    assert (s.other_added, s.other_removed, s.files_changed) == (1, 1, 4)
    assert s.source_files == ("app/notes.py", "web/api.ts")
    assert s.test_files == ("tests/test_notes.py",)
    # notes.py has a matching test change; api.ts doesn't
    assert s.untested_source_files == ("web/api.ts",)


def test_no_test_change_means_every_source_file_is_untested():
    s = compute_stats(parse_diff(_file("a.py", 1) + _file("b.py", 1)), CTX)
    assert s.untested_source_files == ("a.py", "b.py")


def test_deleted_source_file_is_not_untested():
    s = compute_stats(parse_diff(_file("gone.py", 0, 3, deleted=True)), CTX)
    assert s.src_removed == 3 and s.untested_source_files == ()


def test_custom_globs():
    ctx = ContextSection(test_globs=["spec/**"], source_globs=["**/*.rb"])
    s = compute_stats(parse_diff(_file("lib/user.rb", 2) + _file("spec/user_spec.rb", 2)), ctx)
    assert s.source_files == ("lib/user.rb",) and s.test_files == ("spec/user_spec.rb",)
    assert s.untested_source_files == ()


def test_extract_test_diff_only_contains_test_files():
    diff = _file("app/notes.py", 1) + _file("tests/test_notes.py", 1)
    out = extract_test_diff(parse_diff(diff), CTX)
    assert "tests/test_notes.py" in out and "app/notes.py" not in out


def test_render_mentions_untested():
    text = compute_stats(parse_diff(_file("a.py", 1)), CTX).render()
    assert "source lines: +1 -0 in 1 file(s)" in text
    assert "no matching test change: a.py" in text
