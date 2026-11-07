"""The notes service's own unit tests.

Deliberately partial, like most real suites: they are the visible tests a PR author runs. The hidden holdouts
(written from docs/SPEC.md) are what settle a market.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from notes_app import create_app
from notes_app.auth import bearer_token

TOKEN = "test-admin-token"
ADMIN = {"Authorization": f"Bearer {TOKEN}"}


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def client(clock: FakeClock) -> TestClient:
    return TestClient(create_app(TOKEN, clock=clock))


def test_health(client: TestClient) -> None:
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_empty_admin_token_is_rejected() -> None:
    with pytest.raises(ValueError):
        create_app("")


def test_create_and_get_note(client: TestClient) -> None:
    r = client.post("/notes", json={"title": "Groceries", "body": "milk"})
    assert r.status_code == 201
    assert r.json() == {"id": 1, "title": "Groceries", "body": "milk"}
    assert client.post("/notes", json={"title": "Second"}).json() == {"id": 2, "title": "Second", "body": ""}
    assert client.get("/notes/1").json() == {"id": 1, "title": "Groceries", "body": "milk"}


def test_get_missing_note_is_404(client: TestClient) -> None:
    r = client.get("/notes/42")
    assert r.status_code == 404
    assert r.json() == {"detail": "note not found"}


@pytest.mark.parametrize(
    "payload",
    [{}, {"title": ""}, {"title": 5}, {"title": "ok", "body": "x" * 2001}, {"title": "ok", "owner": "me"}],
)
def test_create_rejects_invalid_payloads(client: TestClient, payload: dict) -> None:
    assert client.post("/notes", json=payload).status_code == 422
    assert client.get("/notes").json() == []


def test_list_is_ordered_and_limited(client: TestClient) -> None:
    for i in range(5):
        client.post("/notes", json={"title": f"n{i}"})
    assert [n["id"] for n in client.get("/notes").json()] == [1, 2, 3, 4, 5]
    assert [n["id"] for n in client.get("/notes", params={"limit": 2}).json()] == [1, 2]


@pytest.mark.parametrize("limit", ["0", "101", "abc"])
def test_list_rejects_bad_limits(client: TestClient, limit: str) -> None:
    assert client.get("/notes", params={"limit": limit}).status_code == 422


def test_admin_delete(client: TestClient) -> None:
    client.post("/notes", json={"title": "temp"})
    assert client.delete("/notes/1", headers=ADMIN).status_code == 204
    assert client.get("/notes/1").status_code == 404
    assert client.delete("/notes/1", headers=ADMIN).status_code == 404


def test_admin_stats(client: TestClient) -> None:
    client.post("/notes", json={"title": "a"})
    client.post("/notes", json={"title": "b"})
    client.delete("/notes/1", headers=ADMIN)
    r = client.get("/admin/stats", headers=ADMIN)
    assert r.status_code == 200
    assert r.json() == {"note_count": 1, "total_created": 2, "total_deleted": 1}


def test_admin_stats_requires_auth(client: TestClient) -> None:
    assert client.get("/admin/stats").status_code == 401
    assert client.get("/admin/stats", headers={"Authorization": "Bearer nope"}).status_code == 401


def test_rate_limit_window(client: TestClient, clock: FakeClock) -> None:
    for i in range(10):
        assert client.post("/notes", json={"title": f"n{i}"}).status_code == 201
    r = client.post("/notes", json={"title": "too many"})
    assert r.status_code == 429
    assert r.json() == {"detail": "rate limit exceeded"}
    clock.now = 60.0
    assert client.post("/notes", json={"title": "next window"}).status_code == 201


@pytest.mark.parametrize(
    ("header", "expected"),
    [(None, None), ("Bearer abc", "abc"), ("bearer abc", "abc"), ("Basic abc", None), ("Bearer", None)],
)
def test_bearer_token_parsing(header: str | None, expected: str | None) -> None:
    assert bearer_token(header) == expected
