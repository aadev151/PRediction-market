"""WP A. Engine/session factory from RuntimeEnv.database_url (SQLite; Postgres via DATABASE_URL). PLAN §5."""

from __future__ import annotations

from typing import Any

from sqlalchemy import DDL, Engine, create_engine, event, inspect, make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from prm.config import RuntimeEnv
from prm.models import Base


def _sqlite_pragmas(dbapi_conn: Any, _record: Any) -> None:
    cur = dbapi_conn.cursor()
    cur.execute("PRAGMA foreign_keys=ON")
    cur.close()


def is_sqlite_memory(url: str) -> bool:
    """True for every spelling of an in-memory SQLite URL (``sqlite://``, ``sqlite+pysqlite:///:memory:``,
    ``sqlite:///file:x?mode=memory&uri=true``)."""
    u = make_url(url)
    if u.get_backend_name() != "sqlite":
        return False
    return u.database in (None, "", ":memory:") or u.query.get("mode") == "memory"


def make_engine(url: str) -> Engine:
    """SQLAlchemy engine. SQLite gets ``check_same_thread=False`` and foreign keys ON for every connection."""
    kwargs: dict[str, Any] = {}
    if make_url(url).get_backend_name() == "sqlite":
        kwargs["connect_args"] = {"check_same_thread": False}
        if is_sqlite_memory(url):
            kwargs["poolclass"] = StaticPool  # one shared in-memory database, not one per connection
    engine = create_engine(url, **kwargs)
    if engine.dialect.name == "sqlite":
        event.listen(engine, "connect", _sqlite_pragmas)
    return engine


def init_db(engine: Engine) -> None:
    """Create any missing tables, then add any missing *nullable* columns to existing ones. Idempotent.

    ``create_all`` never alters a table, so a database made before a column was added would fail every query
    on that table. Only nullable columns are added (existing rows get NULL); anything else is left alone.
    """
    Base.metadata.create_all(engine)
    insp = inspect(engine)
    quote = engine.dialect.identifier_preparer.quote
    with engine.begin() as conn:
        for table in Base.metadata.sorted_tables:
            have = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name not in have and col.nullable and not col.primary_key:
                    # Identifiers come from our own metadata (never user input) and are quoted anyway.
                    col_type = col.type.compile(engine.dialect)
                    ddl = f"ALTER TABLE {quote(table.name)} ADD COLUMN {quote(col.name)} {col_type}"
                    conn.execute(DDL(ddl))


def make_session_factory(source: str | RuntimeEnv | Engine) -> sessionmaker[Session]:
    """Session factory for a URL, a ``RuntimeEnv`` or an existing engine. Creates missing tables."""
    if isinstance(source, Engine):
        engine = source
    else:
        engine = make_engine(source.database_url if isinstance(source, RuntimeEnv) else source)
    init_db(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)
