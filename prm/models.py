"""WP A. SQLAlchemy models: repos, traders, markets, beliefs, trades, positions, events, league_runs (§5).

Money is stored as floats (credits, not dollars). ``markets.state`` holds a ``prm.types.State`` value and is
only ever written by ``prm.market.state.transition``. ``events`` is append-only: the ORM refuses updates and
deletes, and on SQLite triggers refuse them at the database too.
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import DDL, DateTime, ForeignKey, String, Text, UniqueConstraint, event
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Repo(Base):
    __tablename__ = "repos"
    id: Mapped[int] = mapped_column(primary_key=True)
    full_name: Mapped[str] = mapped_column(String(200), unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Trader(Base):
    """Agent traders belong to a repo (bankrolls are per repo); humans have ``repo_id`` NULL."""

    __tablename__ = "traders"
    __table_args__ = (UniqueConstraint("repo_id", "name"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int | None] = mapped_column(ForeignKey("repos.id"))
    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(10))  # 'agent' | 'human'
    model: Mapped[str | None] = mapped_column(String(100))
    bankroll: Mapped[float]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class LeagueRun(Base):
    __tablename__ = "league_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    repo_label: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    params_json: Mapped[str] = mapped_column(Text, default="{}")
    report_json: Mapped[str | None] = mapped_column(Text)


class Market(Base):
    __tablename__ = "markets"
    __table_args__ = (UniqueConstraint("repo_id", "pr_number", "head_sha", "source"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    repo_id: Mapped[int] = mapped_column(ForeignKey("repos.id"))
    source: Mapped[str] = mapped_column(String(10))  # 'github' | 'league' | 'demo'
    pr_number: Mapped[int | None]
    head_sha: Mapped[str | None] = mapped_column(String(64))
    base_sha: Mapped[str | None] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(20))
    b: Mapped[float]
    q_yes: Mapped[float]
    q_no: Mapped[float]
    initial_price: Mapped[float]
    final_price: Mapped[float | None]
    gate_state: Mapped[str | None] = mapped_column(String(10))
    gate_description: Mapped[str | None] = mapped_column(String(200))
    outcome: Mapped[str | None] = mapped_column(String(3))
    holdouts_passed: Mapped[int | None]
    holdouts_total: Mapped[int | None]
    findings_json: Mapped[str | None] = mapped_column(Text)
    cost_usd: Mapped[float] = mapped_column(default=0.0)
    latency_ms: Mapped[int] = mapped_column(default=0)
    diff_text: Mapped[str] = mapped_column(Text, default="")
    description_text: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    league_run_id: Mapped[int | None] = mapped_column(ForeignKey("league_runs.id"))


class Belief(Base):
    __tablename__ = "beliefs"
    id: Mapped[int] = mapped_column(primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), index=True)
    trader_id: Mapped[int] = mapped_column(ForeignKey("traders.id"))
    p_yes: Mapped[float | None]
    rationale: Mapped[str] = mapped_column(Text, default="")
    key_risks_json: Mapped[str] = mapped_column(Text, default="[]")
    model: Mapped[str] = mapped_column(String(100), default="")
    input_tokens: Mapped[int] = mapped_column(default=0)
    output_tokens: Mapped[int] = mapped_column(default=0)
    latency_ms: Mapped[int] = mapped_column(default=0)
    cost_usd: Mapped[float] = mapped_column(default=0.0)
    error: Mapped[str | None] = mapped_column(Text)


class Trade(Base):
    __tablename__ = "trades"
    id: Mapped[int] = mapped_column(primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), index=True)
    trader_id: Mapped[int] = mapped_column(ForeignKey("traders.id"))
    round: Mapped[int]
    side: Mapped[str] = mapped_column(String(3))
    shares: Mapped[float]
    cost: Mapped[float]
    price_before: Mapped[float]
    price_after: Mapped[float]
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Position(Base):
    """One trader's stake in one market. ``cap`` is fixed when the position opens; ``payout`` is set once."""

    __tablename__ = "positions"
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), primary_key=True)
    trader_id: Mapped[int] = mapped_column(ForeignKey("traders.id"), primary_key=True)
    yes_shares: Mapped[float] = mapped_column(default=0.0)
    no_shares: Mapped[float] = mapped_column(default=0.0)
    spent: Mapped[float] = mapped_column(default=0.0)
    cap: Mapped[float] = mapped_column(default=0.0)
    payout: Mapped[float | None]


class Event(Base):
    """Audit log of every state transition and trade. Append-only."""

    __tablename__ = "events"
    id: Mapped[int] = mapped_column(primary_key=True)
    market_id: Mapped[int] = mapped_column(ForeignKey("markets.id"), index=True)
    from_state: Mapped[str | None] = mapped_column(String(20))
    to_state: Mapped[str] = mapped_column(String(20))
    actor: Mapped[str] = mapped_column(String(100))
    detail: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AppendOnlyError(RuntimeError):
    """Raised on any attempt to update or delete an audit event."""


@event.listens_for(Event, "before_update")
@event.listens_for(Event, "before_delete")
def _refuse_event_mutation(_mapper: object, _conn: object, target: Event) -> None:
    raise AppendOnlyError(f"events is append-only (event id={target.id})")


for _ddl in (
    "CREATE TRIGGER events_no_update BEFORE UPDATE ON events BEGIN SELECT RAISE(ABORT, 'append-only'); END",
    "CREATE TRIGGER events_no_delete BEFORE DELETE ON events BEGIN SELECT RAISE(ABORT, 'append-only'); END",
):
    event.listen(Event.__table__, "after_create", DDL(_ddl).execute_if(dialect="sqlite"))
