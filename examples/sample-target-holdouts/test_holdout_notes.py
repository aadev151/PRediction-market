"""FIXTURE holdouts for the sample target. Public on purpose so CI can run end to end.

Written from docs/SPEC.md only. Real targets keep holdouts in a private repo.

Covers construction (SPEC §1), the note shape and ids (§2), read/list/delete, and routing (§3).
"""

import pytest
from fastapi.testclient import TestClient
from notes_app import create_app

# The fixture's leak canary. It lives only in the holdouts, so it must never appear in any trader prompt;
# finding it in one means holdout content escaped the settlement sandbox.
FIXTURE_CANARY = "PRM-FIXTURE-CANARY-7f3a91c2"

NOT_FOUND = {"detail": "note not found"}


def test_create_app_rejects_empty_admin_token():
    with pytest.raises(ValueError):
        create_app("")


def test_health_is_ok(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_create_and_read_back_exactly_as_sent(client):
    r = client.post("/notes", json={"title": "Groceries"})
    assert r.status_code == 201
    assert r.json() == {"id": 1, "title": "Groceries", "body": ""}

    raw = {"title": "  spaced\ttitle  ", "body": "\n line one\n  line two  "}
    r = client.post("/notes", json=raw)
    assert r.status_code == 201
    assert r.json() == {"id": 2, **raw}

    assert client.get("/notes/2").json() == {"id": 2, **raw}
    r = client.get("/notes/1")
    assert r.status_code == 200
    assert r.json() == {"id": 1, "title": "Groceries", "body": ""}


def test_ids_increase_by_one_and_are_never_reused(client, create, admin):
    assert [create(f"n{i}")["id"] for i in range(3)] == [1, 2, 3]
    assert client.delete("/notes/3", headers=admin).status_code == 204
    assert create("after deleting the newest")["id"] == 4
    assert client.delete("/notes/1", headers=admin).status_code == 204
    assert create("after deleting the oldest")["id"] == 5


def test_unknown_ids_are_404(client, create):
    create("the only note")
    for note_id in (0, -1, 2, 10**9):
        r = client.get(f"/notes/{note_id}")
        assert r.status_code == 404, note_id
        assert r.json() == NOT_FOUND


def test_delete_really_removes_the_note(client, create, admin):
    keep, gone = create("keep"), create("gone")
    r = client.delete(f"/notes/{gone['id']}", headers=admin)
    assert r.status_code == 204
    assert r.content == b""

    r = client.get(f"/notes/{gone['id']}")
    assert (r.status_code, r.json()) == (404, NOT_FOUND)
    r = client.delete(f"/notes/{gone['id']}", headers=admin)
    assert (r.status_code, r.json()) == (404, NOT_FOUND)
    r = client.delete("/notes/999", headers=admin)
    assert (r.status_code, r.json()) == (404, NOT_FOUND)

    assert client.get("/notes").json() == [keep]
    assert client.get(f"/notes/{keep['id']}").json() == keep


def test_list_ascending_default_limit_and_skips_deleted(client, create, admin):
    notes = [create(f"note {i}", body=f"body {i}") for i in range(25)]
    assert client.get("/notes").json() == notes[:20]
    assert client.get("/notes", params={"limit": 100}).json() == notes
    assert client.get("/notes", params={"limit": 1}).json() == notes[:1]
    assert client.get("/notes", params={"limit": 7}).json() == notes[:7]

    for note_id in (1, 3):
        assert client.delete(f"/notes/{note_id}", headers=admin).status_code == 204
    remaining = [n for n in notes if n["id"] not in (1, 3)]
    assert client.get("/notes", params={"limit": 3}).json() == remaining[:3]
    assert client.get("/notes").json() == remaining[:20]


def test_each_app_is_independent(clock, admin_key):
    a = TestClient(create_app(admin_key, clock=clock))
    b = TestClient(create_app(admin_key, clock=clock))
    for i in range(10):
        assert a.post("/notes", json={"title": f"a{i}"}).status_code == 201
    assert a.post("/notes", json={"title": "a over limit"}).status_code == 429

    r = b.post("/notes", json={"title": "b"})
    assert r.status_code == 201
    assert r.json() == {"id": 1, "title": "b", "body": ""}
    assert b.get("/notes").json() == [{"id": 1, "title": "b", "body": ""}]
    assert a.get("/notes/1").json()["title"] == "a0"

    default_clock = TestClient(create_app(admin_key))
    assert default_clock.post("/notes", json={"title": "real clock"}).json()["id"] == 1


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("PUT", "/notes/1"),
        ("PATCH", "/notes/1"),
        ("POST", "/notes/1"),
        ("PUT", "/notes"),
        ("DELETE", "/notes"),
        ("POST", "/health"),
        ("POST", "/admin/stats"),
    ],
)
def test_other_methods_are_405(client, create, method, path):
    create("exists")
    assert client.request(method, path).status_code == 405


def test_unknown_paths_are_404(client):
    for path in ("/nope", "/notes/1/extra", "/admin"):
        assert client.get(path).status_code == 404, path
