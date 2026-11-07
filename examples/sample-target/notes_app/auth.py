"""Admin authentication for the notes service: `Authorization: Bearer <admin token>`."""

from __future__ import annotations

import hmac

from fastapi import HTTPException, Request


def bearer_token(header: str | None) -> str | None:
    """The token from an `Authorization: Bearer <token>` header; None if missing or malformed."""
    if header is None:
        return None
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def require_admin(request: Request) -> None:
    """FastAPI dependency: 401 unless the request carries the app's admin token."""
    token = bearer_token(request.headers.get("authorization"))
    if token is None:
        raise HTTPException(status_code=401, detail="unauthorized")
    expected: str = request.app.state.admin_token
    if not hmac.compare_digest(token.encode(), expected.encode()):
        raise HTTPException(status_code=401, detail="unauthorized")
