"""FIXTURE holdouts for the sample target. Public on purpose so CI can run end to end.

Written from docs/SPEC.md only. Real targets keep holdouts in a private repo.

Covers the POST /notes rate limit (SPEC §4): 10 accepted per client per sliding 60 s window, fake clock.
"""

from fastapi.testclient import TestClient

LIMITED = {"detail": "rate limit exceeded"}


def _post(client: TestClient, title: str = "t") -> int:
    return client.post("/notes", json={"title": title}).status_code


def test_eleventh_request_is_429_until_the_window_passes(client, clock, admin):
    clock.now = 0
    assert [_post(client) for _ in range(10)] == [201] * 10
    r = client.post("/notes", json={"title": "eleventh"})
    assert (r.status_code, r.json()) == (429, LIMITED)
    clock.now = 59.999
    assert _post(client) == 429
    clock.now = 60
    r = client.post("/notes", json={"title": "next window"})
    assert r.status_code == 201
    assert r.json()["id"] == 11  # the 429s created nothing and consumed no id
    stats = client.get("/admin/stats", headers=admin).json()
    assert stats["total_created"] == 11
    assert len(client.get("/notes", params={"limit": 100}).json()) == 11


def test_window_slides_per_request(client, clock):
    clock.now = 0.0
    assert [_post(client) for _ in range(5)] == [201] * 5
    clock.now = 30.0
    assert [_post(client) for _ in range(5)] == [201] * 5
    clock.now = 59.0
    assert _post(client) == 429
    clock.now = 60.0  # the five from t=0 expire; the five from t=30 still count
    assert [_post(client) for _ in range(6)] == [201] * 5 + [429]
    clock.now = 89.999
    assert _post(client) == 429
    clock.now = 90.0  # the five from t=30 expire
    assert [_post(client) for _ in range(6)] == [201] * 5 + [429]


def test_rejected_requests_do_not_count(client, clock):
    clock.now = 0
    for _ in range(15):
        assert client.post("/notes", json={"title": ""}).status_code == 422
    assert [_post(client) for _ in range(10)] == [201] * 10
    clock.now = 30
    assert [_post(client) for _ in range(5)] == [429] * 5
    clock.now = 60  # only the ten accepted at t=0 counted, and they have now expired
    assert [_post(client) for _ in range(11)] == [201] * 10 + [429]


def test_validation_runs_before_the_rate_limit(client, clock):
    clock.now = 0
    assert [_post(client) for _ in range(10)] == [201] * 10
    assert _post(client) == 429
    assert client.post("/notes", json={"title": ""}).status_code == 422
    assert client.post("/notes", json={"title": "t", "extra": 1}).status_code == 422
    r = client.post("/notes", content=b"{oops", headers={"Content-Type": "application/json"})
    assert r.status_code == 422


def test_clients_are_limited_independently(app, clock):
    first = TestClient(app)
    second = TestClient(app, client=("10.0.0.2", 50000))
    third = TestClient(app, client=("10.0.0.3", 50000))
    clock.now = 0
    assert [_post(first) for _ in range(11)] == [201] * 10 + [429]
    assert [_post(second) for _ in range(11)] == [201] * 10 + [429]
    assert _post(third) == 201
    assert _post(first) == 429
    assert len(first.get("/notes", params={"limit": 100}).json()) == 21  # one shared store


def test_limit_applies_only_to_post_and_deleting_frees_nothing(client, clock, admin):
    clock.now = 0
    assert [_post(client) for _ in range(11)] == [201] * 10 + [429]
    assert client.get("/health").status_code == 200
    assert client.get("/notes").status_code == 200
    assert client.get("/notes/1").status_code == 200
    assert client.get("/admin/stats", headers=admin).status_code == 200
    assert client.delete("/notes/1", headers=admin).status_code == 204
    assert _post(client) == 429  # accepted requests still count after their note is deleted


def test_clock_is_read_once_per_valid_post(client, clock):
    clock.now = 0
    start = clock.calls
    assert client.post("/notes", json={"title": ""}).status_code == 422
    assert client.post("/notes", json={"nope": 1}).status_code == 422
    client.get("/health")
    client.get("/notes")
    client.get("/notes/1")
    assert clock.calls == start
    assert [_post(client) for _ in range(11)] == [201] * 10 + [429]
    assert clock.calls == start + 11
