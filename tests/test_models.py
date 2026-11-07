import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from prm.db import make_session_factory
from prm.models import AppendOnlyError, Event, Market, Position, Repo, Trader
from prm.types import State


@pytest.fixture
def session():
    with make_session_factory("sqlite://")() as s:
        yield s


def _market(repo_id: int, **kw) -> Market:
    fields = dict(
        repo_id=repo_id, source="github", pr_number=1, head_sha="abc", state=State.CREATED.value,
        b=20.0, q_yes=0.0, q_no=0.0, initial_price=0.5,
    )  # fmt: skip
    fields.update(kw)
    return Market(**fields)


def _seed(s) -> tuple[Repo, Market]:
    repo = Repo(full_name="acme/widgets")
    s.add(repo)
    s.flush()
    m = _market(repo.id)
    s.add(m)
    s.flush()
    return repo, m


def test_market_identity_is_unique_for_idempotency(session):
    repo, _ = _seed(session)
    session.add(_market(repo.id))
    with pytest.raises(IntegrityError):
        session.flush()


def test_same_pr_different_source_or_sha_is_a_new_market(session):
    repo, _ = _seed(session)
    session.add_all([_market(repo.id, source="league"), _market(repo.id, head_sha="def")])
    session.flush()
    assert session.query(Market).count() == 3


def test_agent_trader_names_are_unique_per_repo_but_not_across_repos(session):
    a = Repo(full_name="a/a")
    b = Repo(full_name="b/b")
    session.add_all([a, b])
    session.flush()
    session.add_all(
        [
            Trader(repo_id=a.id, name="skeptic", kind="agent", model="m", bankroll=100.0),
            Trader(repo_id=b.id, name="skeptic", kind="agent", model="m", bankroll=100.0),
            Trader(repo_id=None, name="judge", kind="human", model=None, bankroll=100.0),
        ]
    )
    session.flush()
    session.add(Trader(repo_id=a.id, name="skeptic", kind="agent", model="m", bankroll=100.0))
    with pytest.raises(IntegrityError):
        session.flush()


def test_foreign_keys_are_enforced(session):
    session.add(_market(repo_id=999))
    with pytest.raises(IntegrityError):
        session.flush()


def test_position_has_composite_primary_key(session):
    repo, m = _seed(session)
    t = Trader(repo_id=repo.id, name="coverage", kind="agent", model="m", bankroll=100.0)
    session.add(t)
    session.flush()
    session.add(Position(market_id=m.id, trader_id=t.id, cap=25.0))
    session.flush()
    pos = session.get(Position, (m.id, t.id))
    assert pos is not None and pos.spent == 0.0 and pos.payout is None


def _event(s, m) -> Event:
    e = Event(market_id=m.id, from_state="CREATED", to_state="CONTEXT_READY", actor="engine")
    s.add(e)
    s.flush()
    return e


def test_events_refuse_orm_update(session):
    _, m = _seed(session)
    e = _event(session, m)
    e.detail = "rewritten history"
    with pytest.raises(AppendOnlyError):
        session.flush()


def test_events_refuse_orm_delete(session):
    _, m = _seed(session)
    e = _event(session, m)
    session.delete(e)
    with pytest.raises(AppendOnlyError):
        session.flush()


@pytest.mark.parametrize("sql", ["UPDATE events SET detail = 'x'", "DELETE FROM events"])
def test_events_refuse_raw_sql_mutation(session, sql):
    _, m = _seed(session)
    _event(session, m)
    with pytest.raises(IntegrityError, match="append-only"):
        session.execute(text(sql))


def test_market_identity_includes_attempt_so_a_void_market_can_be_retried(session):
    repo, _ = _seed(session)
    session.add(_market(repo.id, attempt=2))
    session.flush()
    session.add(_market(repo.id, attempt=2))
    with pytest.raises(IntegrityError):
        session.flush()


@pytest.fixture
def file_sf(tmp_path):
    """File-backed SQLite: separate connections really see each other's commits (no StaticPool)."""
    return make_session_factory(f"sqlite:///{tmp_path / 'race.db'}")


def test_two_sessions_settling_the_same_gated_market_pay_exactly_once(file_sf):
    with file_sf.begin() as s:
        repo = Repo(full_name="acme/widgets")
        s.add(repo)
        s.flush()
        m = _market(repo.id, state=State.GATED.value)
        t = Trader(repo_id=repo.id, name="skeptic", kind="agent", model="m", bankroll=100.0)
        s.add_all([m, t])
        s.flush()
        mid, tid = m.id, t.id
    s1, s2 = file_sf(), file_sf()
    # Both read GATED before either writes (hold the rows: the identity map only keeps referenced objects).
    held = {s: (s.get(Market, mid), s.get(Trader, tid)) for s in (s1, s2)}
    assert all(m.state == State.GATED.value for m, _ in held.values())

    def pay(s) -> None:
        market, trader = held[s]
        market.state = State.SETTLED.value
        trader.bankroll += 10.0
        s.commit()

    pay(s1)
    with pytest.raises(StaleDataError):
        pay(s2)
    s2.rollback()
    with file_sf() as s:
        assert s.get(Trader, tid).bankroll == 100.0 + 10.0
    s1.close()
    s2.close()


@pytest.mark.parametrize("model", [Trader, Position])
def test_concurrent_read_modify_write_cannot_lose_an_update(file_sf, model):
    with file_sf.begin() as s:
        repo = Repo(full_name="acme/widgets")
        s.add(repo)
        s.flush()
        m = _market(repo.id)
        t = Trader(repo_id=None, name="judge", kind="human", model=None, bankroll=100.0)
        s.add_all([m, t])
        s.flush()
        s.add(Position(market_id=m.id, trader_id=t.id, cap=25.0))
        key = t.id if model is Trader else (m.id, t.id)
    field = "bankroll" if model is Trader else "spent"
    s1, s2 = file_sf(), file_sf()
    rows = [s.get(model, key) for s in (s1, s2)]
    for row in rows:
        setattr(row, field, getattr(row, field) + 5.0)
    s1.commit()
    with pytest.raises(StaleDataError):
        s2.commit()
    s2.rollback()
    with file_sf() as s:
        assert getattr(s.get(model, key), field) in (105.0, 5.0)  # one +5 landed, the other was refused
    s1.close()
    s2.close()
