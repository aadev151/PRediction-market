from pathlib import Path

from prm.config import parse_target_config
from prm.types import PRBundle

TARGET_TOML = """
[target]
name = "fixture"
holdouts_repo = "acme/fixture-holdouts"
spec = "docs/SPEC.md"

[settle]
command = "pytest {holdouts} --junitxml={junit}"
"""

BUG_DIFF = """\
diff --git a/app/notes.py b/app/notes.py
--- a/app/notes.py
+++ b/app/notes.py
@@ -20,5 +20,3 @@ def delete_note(note_id, request):
     note = store.get(note_id)
-    if not is_admin(request):
-        raise HTTPException(401)
     store.delete(note_id)
     return Response(status_code=204)
diff --git a/tests/test_misc.py b/tests/test_misc.py
--- a/tests/test_misc.py
+++ b/tests/test_misc.py
@@ -1,2 +1,3 @@
 def test_health():
     assert True
+# TEST_DIFF_MARKER
"""

GOOD_DIFF = """\
diff --git a/app/notes.py b/app/notes.py
--- a/app/notes.py
+++ b/app/notes.py
@@ -1,2 +1,3 @@
 def get_note(note_id):
+    \"\"\"Return the note or raise 404.\"\"\"
     return store[note_id]
"""

DESCRIPTION = "DESCRIPTION_MARKER: tidy the delete handler"
SPEC = "SPEC_MARKER: DELETE /notes/{id} requires an admin bearer token."


def make_bundle(tmp: Path, diff: str = BUG_DIFF, description: str = DESCRIPTION, spec: str | None = SPEC):
    base, head = tmp / "base", tmp / "head"
    (base / "docs").mkdir(parents=True, exist_ok=True)
    head.mkdir(exist_ok=True)
    if spec is not None:
        (base / "docs" / "SPEC.md").write_text(spec, encoding="utf-8")
    return PRBundle(
        source="league",
        repo="acme/fixture",
        pr_number=7,
        head_sha="feedfacefeedface",
        base_sha="deadbeefdeadbeef",
        diff=diff,
        description=description,
        base_dir=base,
        head_dir=head,
        target=parse_target_config(TARGET_TOML),
    )
