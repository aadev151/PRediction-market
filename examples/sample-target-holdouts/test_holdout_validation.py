"""FIXTURE holdouts for the sample target. Public on purpose so CI can run end to end.

Written from docs/SPEC.md only. Real targets keep holdouts in a private repo.

Covers input validation (SPEC §2, §3): title/body bounds, types, unknown keys, bad bodies, id/limit params.
"""

import json

import pytest

JSON = {"Content-Type": "application/json"}


def _j(obj: object) -> bytes:
    return json.dumps(obj).encode()


@pytest.mark.parametrize(
    ("title", "body"),
    [
        ("x", None),
        (" ", ""),
        ("t" * 100, "b" * 2000),
        ("é" * 100, "é" * 2000),
        ("😀" * 100, "😀" * 2000),
    ],
    ids=["min", "whitespace-only", "ascii-max", "accented-max", "emoji-max"],
)
def test_boundary_values_are_accepted(client, title, body):
    payload = {"title": title} if body is None else {"title": title, "body": body}
    r = client.post("/notes", json=payload)
    assert r.status_code == 201, r.text
    expected = {"id": 1, "title": title, "body": body or ""}
    assert r.json() == expected
    assert client.get("/notes/1").json() == expected


@pytest.mark.parametrize(
    "raw",
    [
        pytest.param(_j({}), id="empty-object"),
        pytest.param(_j({"body": "no title"}), id="missing-title"),
        pytest.param(_j({"title": ""}), id="title-empty"),
        pytest.param(_j({"title": "t" * 101}), id="title-101"),
        pytest.param(_j({"title": "😀" * 101}), id="title-101-emoji"),
        pytest.param(_j({"title": "t", "body": "b" * 2001}), id="body-2001"),
        pytest.param(_j({"title": "t", "body": "é" * 2001}), id="body-2001-accented"),
        pytest.param(_j({"title": 1}), id="title-int"),
        pytest.param(_j({"title": 1.5}), id="title-float"),
        pytest.param(_j({"title": None}), id="title-null"),
        pytest.param(_j({"title": True}), id="title-bool"),
        pytest.param(_j({"title": ["t"]}), id="title-list"),
        pytest.param(_j({"title": {"t": "t"}}), id="title-object"),
        pytest.param(_j({"title": "t", "body": None}), id="body-null"),
        pytest.param(_j({"title": "t", "body": 0}), id="body-int"),
        pytest.param(_j({"title": "t", "id": 7}), id="unknown-key-id"),
        pytest.param(_j({"title": "t", "body": "", "tags": []}), id="unknown-key-tags"),
        pytest.param(_j([{"title": "t"}]), id="array"),
        pytest.param(_j("t"), id="string"),
        pytest.param(_j(5), id="number"),
        pytest.param(b"null", id="null"),
        pytest.param(b'{"title": "t",}', id="trailing-comma"),
        pytest.param(b"{not json", id="not-json"),
        pytest.param(b"", id="empty-body"),
    ],
)
def test_invalid_create_is_422_and_creates_nothing(client, admin, raw):
    r = client.post("/notes", content=raw, headers=JSON)
    assert r.status_code == 422, r.text
    assert "detail" in r.json()
    assert client.get("/notes").json() == []
    stats = client.get("/admin/stats", headers=admin).json()
    assert stats == {"note_count": 0, "total_created": 0, "total_deleted": 0}
    # The rejected request consumed no id.
    assert client.post("/notes", json={"title": "ok"}).json()["id"] == 1


@pytest.mark.parametrize("bad_id", ["abc", "1.5", "one"])
def test_non_integer_note_id_is_422(client, create, admin, bad_id):
    create("exists")
    assert client.get(f"/notes/{bad_id}").status_code == 422
    assert client.delete(f"/notes/{bad_id}", headers=admin).status_code == 422
    assert len(client.get("/notes").json()) == 1


@pytest.mark.parametrize("limit", ["0", "-1", "101", "1000", "abc", "1.5"])
def test_list_limit_out_of_range_or_not_integer_is_422(client, create, limit):
    create("exists")
    assert client.get("/notes", params={"limit": limit}).status_code == 422
    for ok in ("1", "100"):
        assert client.get("/notes", params={"limit": ok}).status_code == 200
