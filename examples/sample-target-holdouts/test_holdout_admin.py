"""FIXTURE holdouts for the sample target. Public on purpose so CI can run end to end.

Written from docs/SPEC.md only. Real targets keep holdouts in a private repo.

Covers admin auth (SPEC §5) on both admin endpoints, auth-first on DELETE, and the /admin/stats counters (§3).
"""

import pytest
from fastapi.testclient import TestClient
from notes_app import create_app

UNAUTHORIZED = {"detail": "unauthorized"}

# Templates for bad Authorization headers; {key} is the real admin key. None means no header at all.
BAD_AUTH = [
    pytest.param(None, id="missing"),
    pytest.param("Basic Zml4dHVyZTpwYXNz", id="basic-scheme"),
    pytest.param("Basic {key}", id="basic-scheme-real-key"),
    pytest.param("Token {key}", id="other-scheme"),
    pytest.param("{key}", id="no-scheme"),
    pytest.param("Bearer", id="no-token"),
    pytest.param("Bearer ", id="empty-token"),
    pytest.param("Bearer wrong-key", id="wrong-token"),
    pytest.param("Bearer {lower}", id="token-lowercased"),
    pytest.param("Bearer {upper}", id="token-uppercased"),
    pytest.param("Bearer {key}x", id="token-suffix"),
    pytest.param("Bearer {prefix}", id="token-prefix"),
    pytest.param("Bearer  {key}", id="two-spaces"),
    pytest.param("Bearer {key} ", id="trailing-space"),
]


@pytest.mark.parametrize("template", BAD_AUTH)
def test_admin_endpoints_reject_bad_auth_before_anything_else(client, create, admin_key, template):
    note = create("protected")
    headers = {}
    if template is not None:
        variants = {"lower": admin_key.lower(), "upper": admin_key.upper(), "prefix": admin_key[:-1]}
        headers = {"Authorization": template.format(key=admin_key, **variants)}
    for method, path in [
        ("DELETE", f"/notes/{note['id']}"),  # exists
        ("DELETE", "/notes/999"),  # does not exist
        ("DELETE", "/notes/abc"),  # not an integer
        ("GET", "/admin/stats"),
    ]:
        r = client.request(method, path, headers=headers)
        assert r.status_code == 401, (method, path)
        assert r.json() == UNAUTHORIZED
    assert client.get(f"/notes/{note['id']}").json() == note


@pytest.mark.parametrize("scheme", ["Bearer", "bearer", "BEARER", "bEaReR"])
def test_bearer_scheme_is_case_insensitive(client, create, admin_key, scheme):
    headers = {"Authorization": f"{scheme} {admin_key}"}
    assert client.get("/admin/stats", headers=headers).status_code == 200
    note = create("to delete")
    assert client.delete(f"/notes/{note['id']}", headers=headers).status_code == 204


def test_token_is_exactly_the_one_given_to_create_app(admin_key):
    other = TestClient(create_app("another-admin-key"))
    assert other.get("/admin/stats", headers={"Authorization": f"Bearer {admin_key}"}).status_code == 401
    r = other.get("/admin/stats", headers={"Authorization": "Bearer another-admin-key"})
    assert r.status_code == 200


def test_stats_count_only_successful_changes(client, clock, create, admin):
    def stats() -> dict:
        r = client.get("/admin/stats", headers=admin)
        assert r.status_code == 200
        return r.json()

    assert stats() == {"note_count": 0, "total_created": 0, "total_deleted": 0}
    a, b, _ = create("a"), create("b"), create("c")
    assert client.delete(f"/notes/{b['id']}", headers=admin).status_code == 204
    assert stats() == {"note_count": 2, "total_created": 3, "total_deleted": 1}

    # Rejected requests (4xx) change nothing.
    assert client.post("/notes", json={"title": ""}).status_code == 422
    assert client.delete(f"/notes/{b['id']}", headers=admin).status_code == 404
    assert client.delete(f"/notes/{a['id']}").status_code == 401
    assert client.delete("/notes/abc", headers=admin).status_code == 422
    clock.now += 60
    for _ in range(10):
        assert client.post("/notes", json={"title": "burst"}).status_code == 201
    assert client.post("/notes", json={"title": "one too many"}).status_code == 429
    assert stats() == {"note_count": 12, "total_created": 13, "total_deleted": 1}


def test_public_endpoints_ignore_the_authorization_header(client, admin):
    for headers in ({"Authorization": "Basic not-even-base64"}, {"Authorization": "Bearer wrong"}, admin):
        assert client.get("/health", headers=headers).status_code == 200
        r = client.post("/notes", json={"title": "t"}, headers=headers)
        assert r.status_code == 201
        note_id = r.json()["id"]
        assert client.get(f"/notes/{note_id}", headers=headers).status_code == 200
        assert client.get("/notes", headers=headers).status_code == 200
