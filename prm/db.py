"""WP A. Engine/session factory from RuntimeEnv.database_url (SQLite; Postgres via DATABASE_URL). PLAN §5."""

from __future__ import annotations

from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from prm.config import RuntimeEnv
from prm.models import Base


def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


def make_engine(url: str) -> Engine:
    """SQLAlchemy engine. SQLite gets ``check_same_thread=False`` and foreign keys ON for every connection."""
    kwargs: dict[str, Any] = {}
    if url.startswith("sqlite"):
        kwargs["connect_args"] = {"check_same_thread": False}
        if url in ("sqlite://", "sqlite:///:memory:"):
            kwargs["poolclass"] = StaticPool  # one shared in-memory database, not one per connection
    engine = create_engine(url, **kwargs)
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", _sqlite_pragmas)
    return engine


def init_db(engine: Engine) -> None:
    """Create any missing tables. Idempotent."""
    Base.metadata.create_all(engine)


def make_session_factory(source: str | RuntimeEnv | Engine) -> sessionmaker[Session]:
    """Session factory for a URL, a ``RuntimeEnv`` or an existing engine. Creates missing tables."""
    if isinstance(source, Engine):
        engine = source
    else:
        engine = make_engine(source.database_url if isinstance(source, RuntimeEnv) else source)
    init_db(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)
