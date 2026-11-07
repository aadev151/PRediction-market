"""MarketEngine invariants (PLAN §6.8; R-MKT-2..4, R-SM-1, R-SM-2). The /how page links to these by name."""

import random
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select

from prm.config import ServerConfig, TargetGateOverrides, TraderSpec, parse_target_config
from prm.db import make_session_factory
from prm.market.engine import VOID_DESCRIPTION, MarketEngine, TradeRejected
from prm.market.state import IllegalAction, IllegalTransition
from prm.models import Belief, Event, Market, Trade, Trader
from prm.types import BeliefResult, Finding, PRBundle, SettlementResult, State

TARGET = parse_target_config(
    '[target]\nname = "t"\nholdouts_repo = "o/h"\n[settle]\ncommand = "run {holdouts}"\n'
)
ROLES = ("diff_reader", "coverage", "security", "skeptic")
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
YES = SettlementResult("YES", 5, 5, [Finding("f1", "ok", "low", False)], 10, None)
NO = SettlementResult("NO", 3, 5, [], 10, None)


class Stub:
    """BeliefProvider: a float is a belief, None abstains, an exception is raised."""

    def __init__(self, beliefs):
        self.beliefs = beliefs

    async def get_belief(self, spec: TraderSpec, bundle: PRBundle) -> BeliefResult:
        v = self.beliefs.get(spec.name)
        if isinstance(v, Exception):
            raise v
        if v is None:
            return BeliefResult(spec.name, None, "", [], spec.model, 0, 0, 0, 0.0, "timeout")
        latency = 40 + len(spec.name)
        return BeliefResult(spec.name, v, "why", ["risk"], spec.model, 100, 20, latency, 0.001, None)


def beliefs(*values):
    return Stub(dict(zip(ROLES, values, strict=True)))


def bundle(pr: int = 1, sha: str = "abc", repo: str = "acme/widgets") -> PRBundle:
    return PRBundle("github", repo, pr, sha, "base", "diff --git a b", "desc", Path("."), Path("."), TARGET)


def make(llm, seed: int = 7, cfg: ServerConfig | None = None) -> MarketEngine:
    rng = random.Random(seed)  # noqa: S311 - deterministic trade order, not security
    return MarketEngine(make_session_factory("sqlite://"), cfg or ServerConfig(), llm, lambda: NOW, rng)


async def run(engine: MarketEngine, b: PRBundle | None = None) -> int:
    b = b or bundle()
    mid = engine.create(engine.ensure_repo(b.repo), b)
    await engine.run_agents(mid, b)
    return mid


def market(engine, mid) -> Market:
    with engine._sf() as s:
        return s.get(Market, mid)


def bankrolls(engine, repo_id=None) -> dict[str, float]:
    with engine._sf() as s:
        q = select(Trader).where(Trader.kind == "agent")
        if repo_id is not None:
            q = q.where(Trader.repo_id == repo_id)
        return {t.name: t.bankroll for t in s.scalars(q)}


def trades(engine, mid) -> list[Trade]:
    with engine._sf() as s:
        return list(s.scalars(select(Trade).where(Trade.market_id == mid).order_by(Trade.id)))


async def test_full_lifecycle_create_trade_close_gate_settle():
    e = make(beliefs(0.95, 0.95, 0.95, 0.95))
    mid = await run(e)
    assert market(e, mid).state == State.TRADING
    p = e.close(mid)
    assert 0.9 <= p <= 0.95
    d = e.gate(mid, TargetGateOverrides())
    assert (d.state, d.description, d.price) == ("success", f"auto-merge OK (p={p:.2f})", p)
    before = bankrolls(e)
    payouts = e.settle(mid, YES)
    after = bankrolls(e)
    m = market(e, mid)
    assert m.state == State.SETTLED and m.outcome == "YES" and (m.holdouts_passed, m.holdouts_total) == (5, 5)
    assert m.cost_usd == pytest.approx(0.004) and m.latency_ms == 40 + len("diff_reader")
    assert '"f1"' in m.findings_json
    assert set(payouts) == set(ROLES) and sum(payouts.values()) > 0
    for name, paid in payouts.items():
        assert after[name] == pytest.approx(before[name] + paid)
    with e._sf() as s:
        path = [(ev.from_state, ev.to_state) for ev in s.scalars(select(Event).order_by(Event.id))
                if ev.from_state != ev.to_state]  # fmt: skip
        assert s.query(Belief).count() == 4
    assert path == [
        (None, "CREATED"), ("CREATED", "CONTEXT_READY"), ("CONTEXT_READY", "TRADING"), ("TRADING", "CLOSED"),
        ("CLOSED", "GATED"), ("GATED", "SETTLING"), ("SETTLING", "SETTLED"),
    ]  # fmt: skip


async def test_every_trade_is_logged_in_trades_and_events():
    e = make(beliefs(0.9, 0.2, 0.7, 0.4))
    mid = await run(e)
    with e._sf() as s:
        trade_events = [ev for ev in s.scalars(select(Event)) if ev.from_state == ev.to_state == "TRADING"]
    assert len(trades(e, mid)) == len(trade_events) > 0
    assert {ev.actor for ev in trade_events} <= set(ROLES)


@pytest.mark.parametrize(("p_yes", "state", "label"), [(0.75, "pending", "needs 1 human review"),
                                                       (0.2, "failure", "blocked")])  # fmt: skip
async def test_gate_thresholds(p_yes, state, label):
    e = make(beliefs(p_yes, p_yes, p_yes, p_yes))
    mid = await run(e)
    p = e.close(mid)
    d = e.gate(mid, None)
    assert (d.state, d.description) == (state, f"{label} (p={p:.2f})")


async def test_no_single_trader_can_force_auto_merge():
    """R-MKT-3: one trader at belief 0.98 with a huge bankroll still can't reach 0.90."""
    e = make(beliefs(0.98, 0.98, None, None))
    rid = e.ensure_repo("acme/widgets")
    with e._sf.begin() as s:
        for t in s.scalars(select(Trader).where(Trader.repo_id == rid)):
            t.bankroll = 1e9 if t.name == "diff_reader" else 0.0
    mid = e.create(rid, bundle())
    await e.run_agents(mid, bundle())
    ts = trades(e, mid)
    assert ts and all(t.price_after < 0.9 for t in ts)
    assert sum(t.cost for t in ts) <= 30.0 + 1e-9
    assert sum(t.cost for t in ts) == pytest.approx(30.0)  # spent its whole max_spend_abs
    e.close(mid)
    assert e.gate(mid, None).state != "success"


async def test_fewer_than_two_responding_traders_voids_with_failure_gate():
    e = make(beliefs(0.95, None, None, None))
    mid = await run(e)
    m = market(e, mid)
    assert m.state == State.VOID and m.gate_state == "failure" and m.gate_description == VOID_DESCRIPTION
    assert trades(e, mid) == []
    assert set(bankrolls(e).values()) == {100.0}
    d = e.gate(mid, None)
    assert (d.state, d.price, d.description) == ("failure", None, VOID_DESCRIPTION)
    with pytest.raises(IllegalTransition):
        e.close(mid)


async def test_provider_exception_becomes_abstention_not_crash():
    e = make(beliefs(RuntimeError("boom"), 0.9, ValueError("bad json"), 0.8))
    mid = await run(e)
    assert market(e, mid).state == State.TRADING
    with e._sf() as s:
        errors = {b.error for b in s.scalars(select(Belief).where(Belief.p_yes.is_(None)))}
    assert errors == {"RuntimeError", "ValueError"}


async def test_all_providers_failing_voids_instead_of_crashing():
    e = make(beliefs(*(RuntimeError("down"),) * 4))
    mid = await run(e)
    assert market(e, mid).state == State.VOID


async def test_unexpected_engine_error_voids_market_fail_closed(monkeypatch):
    e = make(beliefs(0.95, 0.95, 0.95, 0.95))

    def explode(*_a, **_k):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(e, "_trade", explode)
    with pytest.raises(RuntimeError):
        await run(e)
    m = market(e, 1)
    assert m.state == State.VOID and m.gate_state == "failure"
    assert e.gate(1, None).state == "failure"


async def test_payouts_happen_exactly_once():
    e = make(beliefs(0.95, 0.9, 0.3, 0.2))
    mid = await run(e)
    e.close(mid)
    e.gate(mid, None)
    first = e.settle(mid, NO)
    after_first = bankrolls(e)
    second = e.settle(mid, NO)
    assert second == first
    assert bankrolls(e) == after_first
    e.void(mid, "late void")  # terminal: no-op, no refund
    assert bankrolls(e) == after_first and market(e, mid).state == State.SETTLED


async def test_void_refunds_exactly_once():
    e = make(beliefs(0.95, 0.9, 0.3, 0.2))
    mid = await run(e)
    assert any(v < 100.0 for v in bankrolls(e).values())
    e.void(mid, "operator")
    assert bankrolls(e) == pytest.approx(dict.fromkeys(ROLES, 100.0))
    e.void(mid, "again")
    assert e.settle(mid, YES) == {}
    assert bankrolls(e) == pytest.approx(dict.fromkeys(ROLES, 100.0))


async def test_settlement_error_voids_and_refunds():
    e = make(beliefs(0.95, 0.9, 0.3, 0.2))
    mid = await run(e)
    e.close(mid)
    e.gate(mid, None)
    assert e.settle(mid, SettlementResult.infra_error("sandbox died")) == {}
    assert market(e, mid).state == State.VOID
    assert bankrolls(e) == pytest.approx(dict.fromkeys(ROLES, 100.0))


async def test_unsafe_gate_overrides_void_fail_closed():
    e = make(beliefs(0.95, 0.95, 0.95, 0.95))
    mid = await run(e)
    e.close(mid)
    d = e.gate(mid, TargetGateOverrides(auto_merge=0.7))
    assert (d.state, d.price) == ("failure", None)
    assert market(e, mid).state == State.VOID


async def test_settle_before_gate_is_illegal():
    e = make(beliefs(0.95, 0.95, 0.95, 0.95))
    mid = await run(e)
    with pytest.raises(IllegalAction):
        e.settle(mid, YES)
    assert market(e, mid).state == State.TRADING


async def test_human_trade_outside_human_window_raises():
    e = make(beliefs(0.6, 0.6, 0.6, 0.6))
    mid = await run(e)
    with e._sf.begin() as s:
        judge = Trader(repo_id=None, name="judge", kind="human", model=None, bankroll=100.0)
        s.add(judge)
        s.flush()
        jid = judge.id
        agent_id = s.scalar(select(Trader.id).where(Trader.kind == "agent"))
    with pytest.raises(IllegalAction):
        e.human_trade(mid, jid, "YES", 5.0)
    assert market(e, mid).state == State.TRADING  # a caller mistake does not void the market
    e.open_human_window(mid)
    with pytest.raises(IllegalAction):
        e.human_trade(mid, agent_id, "YES", 5.0)  # agents don't trade in HUMAN_WINDOW
    with pytest.raises(TradeRejected):
        e.human_trade(mid, jid, "NO", 26.0)  # cap = min(25% of 100, 30) = 25
    r1 = e.human_trade(mid, jid, "NO", 20.0)
    assert r1.side == "NO" and r1.cost == pytest.approx(20.0) and r1.price_after < r1.price_before
    with pytest.raises(TradeRejected):
        e.human_trade(mid, jid, "YES", 5.5)  # cumulative: only 5 left
    e.human_trade(mid, jid, "YES", 5.0)
    with e._sf() as s:
        assert s.get(Trader, jid).bankroll == pytest.approx(75.0)
    e.close(mid)
    with pytest.raises(IllegalAction):
        e.human_trade(mid, jid, "YES", 1.0)


async def test_caps_respected_cumulatively_over_rounds():
    cfg = ServerConfig(market={"rounds": 5})
    e = make(beliefs(0.7, 0.3, 0.7, 0.3), cfg=cfg)
    mid = await run(e)
    ts = trades(e, mid)
    assert max(t.round for t in ts) > 1  # traders really fought over several rounds
    spent: dict[int, float] = {}
    for t in ts:
        spent[t.trader_id] = spent.get(t.trader_id, 0.0) + t.cost
    assert all(v <= 25.0 + 1e-9 for v in spent.values())
    assert max(spent.values()) == pytest.approx(25.0)  # the cap actually binds
    br = bankrolls(e)
    assert sum(100.0 - v for v in br.values()) == pytest.approx(sum(spent.values()))
    assert all(v >= 75.0 - 1e-9 for v in br.values())


async def test_bankrolls_are_per_repo():
    e = make(beliefs(0.95, 0.9, 0.3, 0.2))
    a = e.ensure_repo("acme/a")
    b = e.ensure_repo("acme/b")
    await run(e, bundle(repo="acme/a"))
    assert any(v < 100.0 for v in bankrolls(e, a).values())
    assert bankrolls(e, b) == dict.fromkeys(ROLES, 100.0)


async def test_deterministic_with_seeded_rng():
    def fingerprint(engine, mid):
        return [(t.trader_id, t.round, t.side, round(t.cost, 12)) for t in trades(engine, mid)]

    e1, e2 = make(beliefs(0.9, 0.1, 0.8, 0.3), seed=42), make(beliefs(0.9, 0.1, 0.8, 0.3), seed=42)
    assert fingerprint(e1, await run(e1)) == fingerprint(e2, await run(e2))


def test_create_is_idempotent_per_pr_head_and_source():
    e = make(beliefs(0.9, 0.9, 0.9, 0.9))
    rid = e.ensure_repo("acme/widgets")
    assert e.create(rid, bundle()) == e.create(rid, bundle())
    assert e.create(rid, bundle(sha="def")) != e.create(rid, bundle())
    assert e.ensure_repo("acme/widgets") == rid
