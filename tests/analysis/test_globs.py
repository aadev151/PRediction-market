import pytest

from prm.analysis.globs import matches
from prm.config import ContextSection


@pytest.mark.parametrize(
    ("path", "globs", "expected"),
    [
        ("tests/test_x.py", ["tests/**"], True),
        ("tests/deep/x.py", ["tests/**"], True),
        ("src/tests_helper.py", ["tests/**"], False),
        ("test_notes.py", ["**/test_*.py"], True),  # ** matches zero directories
        ("app/sub/test_notes.py", ["**/test_*.py"], True),
        ("app/notes_test.py", ["**/*_test.py"], True),
        ("web/notes.test.ts", ["**/*.test.ts"], True),
        ("app/auth.py", ["**/auth*"], True),
        ("app/auth/views.py", ["**/auth*"], True),  # a parent directory matches
        ("app/oauthlib.py", ["**/auth*"], False),  # * never crosses /, and "auth" must start the name
        (".github/workflows/ci.yml", [".github/**"], True),
        ("app/x.py", ["*.py"], False),  # * does not cross /
        ("x.py", ["*.py"], True),
        ("a/b.py", ["a/?.py"], True),
        ("a/bc.py", ["a/?.py"], False),
        ("app.py", [], False),
    ],
)
def test_matches(path, globs, expected):
    assert matches(path, globs) is expected


def test_default_context_globs_classify_common_layouts():
    ctx = ContextSection()
    assert matches("tests/test_api.py", ctx.test_globs)
    assert matches("pkg/models_test.py", ctx.test_globs)
    assert not matches("pkg/models.py", ctx.test_globs)
    assert matches("pkg/models.py", ctx.source_globs)
    assert matches("db/migrations/0001.sql", ctx.sensitive_globs)
    assert matches("config/.env.prod", ctx.sensitive_globs)
