"""The notes service: an in-memory notes API with admin auth, validation and a per-client rate limit."""

from __future__ import annotations

import time
from collections import defaultdict, deque
from collections.abc import Callable
from typing import Annotated, Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from pydantic import BaseModel, ConfigDict, Field

from notes_app.auth import require_admin

Clock = Callable[[], float]


class NoteIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str = Field(min_length=1, max_length=100)
    body: str = Field("", max_length=2000)


class RateLimiter:
    """Sliding window: at most `limit` accepted requests per key in any `window_s` seconds."""

    def __init__(self, limit: int, window_s: float, clock: Clock) -> None:
        self._limit = limit
        self._window_s = window_s
        self._clock = clock
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    def allow(self, key: str) -> bool:
        now = self._clock()
        hits = self._hits[key]
        while hits and now - hits[0] >= self._window_s:
            hits.popleft()
        if len(hits) >= self._limit:
            return False
        hits.append(now)
        return True


def create_app(admin_token: str, clock: Clock | None = None) -> FastAPI:
    """Build a fresh app with its own empty store. `clock` returns seconds (default: time.monotonic)."""
    if not admin_token:
        raise ValueError("admin_token must be a non-empty string")

    app = FastAPI(title="notes")
    app.state.admin_token = admin_token
    limiter = RateLimiter(limit=10, window_s=60.0, clock=clock or time.monotonic)
    notes: dict[int, dict[str, Any]] = {}
    counters = {"next_id": 1, "created": 0, "deleted": 0}

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/notes", status_code=201)
    def create_note(note: NoteIn, request: Request) -> dict[str, Any]:
        client = request.client.host if request.client else "unknown"
        if not limiter.allow(client):
            raise HTTPException(status_code=429, detail="rate limit exceeded")
        note_id = counters["next_id"]
        counters["next_id"] += 1
        counters["created"] += 1
        notes[note_id] = {"id": note_id, "title": note.title, "body": note.body}
        return notes[note_id]

    @app.get("/notes/{note_id}")
    def get_note(note_id: int) -> dict[str, Any]:
        note = notes.get(note_id)
        if note is None:
            raise HTTPException(status_code=404, detail="note not found")
        return note

    @app.get("/notes")
    def list_notes(limit: int = 20) -> list[dict[str, Any]]:
        if limit < 1 or limit > 100:
            raise HTTPException(status_code=422, detail="limit must be between 1 and 100")
        return [notes[i] for i in sorted(notes)[:limit]]

    @app.delete("/notes/{note_id}", status_code=204)
    def delete_note(
        note_id: int,
        _admin: Annotated[None, Depends(require_admin)],
    ) -> Response:
        if note_id not in notes:
            raise HTTPException(status_code=404, detail="note not found")
        del notes[note_id]
        counters["deleted"] += 1
        return Response(status_code=204)

    @app.get("/admin/stats")
    def admin_stats(
        _admin: Annotated[None, Depends(require_admin)],
    ) -> dict[str, int]:
        return {
            "note_count": len(notes),
            "total_created": counters["created"],
            "total_deleted": counters["deleted"],
        }

    return app
