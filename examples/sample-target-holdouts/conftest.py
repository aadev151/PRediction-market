"""FIXTURE holdouts for the sample target. Public on purpose so CI can run end to end.

Written from docs/SPEC.md only. Real targets keep holdouts in a private repo.

Shared fixtures: a fake clock the test controls (SPEC §1), a fresh app per test, admin headers (SPEC §5).
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from notes_app import create_app

# Fixture-only admin credential. Mixed case on purpose so the case-sensitivity checks mean something.
ADMIN_KEY = "Fixture-Admin-Key-42"


class FakeClock:
    """Zero-argument clock returning seconds; the test sets `now` and can see how often it was read."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now
        self.calls = 0

    def __call__(self) -> float:
        self.calls += 1
        return self.now


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def admin_key() -> str:
    return ADMIN_KEY


@pytest.fixture
def app(clock: FakeClock, admin_key: str) -> FastAPI:
    return create_app(admin_key, clock=clock)


@pytest.fixture
def client(app: FastAPI) -> TestClient:
    return TestClient(app)


@pytest.fixture
def admin(admin_key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {admin_key}"}


@pytest.fixture
def create(client: TestClient, clock: FakeClock) -> Callable[..., dict[str, Any]]:
    """POST a valid note and return it. Steps the clock 60 s first so setup never trips the rate limit."""

    def _create(title: str = "note", **extra: Any) -> dict[str, Any]:
        clock.now += 60
        r = client.post("/notes", json={"title": title, **extra})
        assert r.status_code == 201, r.text
        return r.json()

    return _create
