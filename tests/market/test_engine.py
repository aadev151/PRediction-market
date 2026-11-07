"""MarketEngine invariants (PLAN §6.8; R-MKT-2..4, R-SM-1, R-SM-2). The /how page links to these by name."""

import asyncio
import random
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.exc import OperationalError

from prm.config import ServerConfig, TargetGateOverrides, TraderSpec, parse_target_config
from prm.db import make_session_factory
from prm.market.engine import (
    MAX_ATTEMPTS,
    VOID_DESCRIPTION,
    MarketAttemptsExhausted,
    MarketEngine,
    TradeConflict,
    TradeRejected,
)
from prm.market.state import IllegalAction, IllegalTransition
from prm.models import Belief, Event, Market, Trade, Trader
from prm.types import BeliefResult, EscalationContext, Finding, PRBundle, SettlementResult, State

TARGET = parse_target_config(
    '[target]\nname = "t"\nholdouts_repo = "o/h"\n[settle]\ncommand = "run {holdouts}"\n'
)
ROLES = ("diff_reader", "coverage", "security", "skeptic")
NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)
YES = SettlementResult("YES", 5, 5, [Finding("f1", "ok", "low", False)], 10, None)
NO = SettlementResult("NO", 3, 5, [], 10, None)


class Stub:
    """BeliefProvider: a float is a belief, None abstains, an exception is raised, "hang" never returns.

    ``escalated`` overrides a trader's answer in escalation rounds (default: the same as its first belief).
    Every call is recorded as (trader, EscalationContext | None).
    """

    def __init__(self, beliefs, escalated=None):
        self.beliefs = beliefs
        self.escalated = escalated or {}
        self.calls: list[tuple[str, EscalationContext | None]] = []

    async def get_belief(
        self, spec: TraderSpec, bundle: PRBundle, escalation: EscalationContext | None = None
    ) -> BeliefResult:
        self.calls.append((spec.name, escalation))
        v = self.beliefs.get(spec.name)
        if escalation is not None and spec.name in self.escalated:
            v = self.escalated[spec.name]
        if isinstance(v, Exception):
            raise v
        if v == "hang":
            await asyncio.Event().wait()
        if v is None:
            return BeliefResult(spec.name, None, "", [], spec.model, 0, 0, 0, 0.0, "timeout")
        latency = 40 + len(spec.name)
        return BeliefResult(
            spec.name, v, f"why {spec.name}", ["risk"], spec.model, 100, 20, latency, 0.001, None
        )


def beliefs(*values):
    return Stub(dict(zip(ROLES, values, strict=True)))


def bundle(
    pr: int = 1, sha: str = "abc", repo: str = "acme/widgets", source="github", target=TARGET
) -> PRBundle:
    return PRBundle(source, repo, pr, sha, "base", "diff --git a b", "desc", Path("."), Path("."), target)


def make(llm, seed: int = 7, cfg: ServerConfig | None = None, url: str = "sqlite://", **kw) -> MarketEngine:
    rng = random.Random(seed)  # noqa: S311 - deterministic trade order, not security
    return MarketEngine(make_session_factory(url), cfg or ServerConfig(), llm, lambda: NOW, rng, **kw)


def file_url(tmp_path) -> str:
    """Race tests need real separate connections (in-memory StaticPool shares one)."""
    return f"sqlite:///{tmp_path / 'engine.db'}"


def add_judge(engine) -> int:
    with engine._sf.begin() as s:
        judge = Trader(repo_id=None, name="judge", kind="human", model=None, bankroll=100.0)
        s.add(judge)
        s.flush()
        return judge.id


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


@pytest.mark.parametrize(("p_yes", "state", "label"), [(0.75, "failure", "no consensus"),
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
    assert market(e, mid).escalations == 2  # in band, so it escalated: caps are cumulative across escalations
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
    e = make(beliefs(0.95, 0.95, 0.95, 0.95))
    mid = await run(e)
    e.close(mid)
    assert e.gate(mid, None).state == "success"  # only merged markets pay out
    first = e.settle(mid, YES)
    assert sum(first.values()) > 0
    after_first = bankrolls(e)
    second = e.settle(mid, YES)
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


def test_create_is_idempotent_per_pr_head_and_source_while_market_is_live():
    e = make(beliefs(0.9, 0.9, 0.9, 0.9))
    rid = e.ensure_repo("acme/widgets")
    assert e.create(rid, bundle()) == e.create(rid, bundle())
    assert e.create(rid, bundle(sha="def")) != e.create(rid, bundle())
    assert e.ensure_repo("acme/widgets") == rid


# ------------------------------------------------------------------------------ code-review regressions


async def test_concurrent_settle_pays_exactly_once(tmp_path, monkeypatch):
    """Two settles both read GATED; the loser's stale write rolls back and it returns the stored payouts."""
    e = make(beliefs(0.95, 0.95, 0.95, 0.95), url=file_url(tmp_path))
    mid = await run(e)
    e.close(mid)
    e.gate(mid, None)
    before = bankrolls(e)
    orig_move, started, raced = e._move, [], []

    def racing_move(s, m, to, detail=""):
        if to == State.SETTLING and not started:
            started.append(True)
            raced.append(e.settle(mid, YES))  # a second request settles while this one holds a GATED read
        orig_move(s, m, to, detail)

    monkeypatch.setattr(e, "_move", racing_move)
    payouts = e.settle(mid, YES)
    assert raced and payouts == raced[0] and sum(payouts.values()) > 0
    assert bankrolls(e) == pytest.approx({n: before[n] + payouts[n] for n in ROLES})
    with e._sf() as s:
        assert s.query(Event).filter_by(market_id=mid, to_state="SETTLED").count() == 1


async def test_concurrent_human_trades_conflict_instead_of_losing_a_debit(tmp_path, monkeypatch):
    e = make(beliefs(0.6, 0.6, 0.6, 0.6), url=file_url(tmp_path))
    mid = await run(e)
    e.open_human_window(mid)
    jid = add_judge(e)
    orig_trade = e._trade

    def racing_trade(*args):
        with e._sf.begin() as other:  # another request debits the same judge first
            other.get(Trader, jid).bankroll -= 10.0
        return orig_trade(*args)

    monkeypatch.setattr(e, "_trade", racing_trade)
    with pytest.raises(TradeConflict):
        e.human_trade(mid, jid, "YES", 5.0)
    with e._sf() as s:
        assert s.get(Trader, jid).bankroll == pytest.approx(90.0)  # the other debit kept, ours rolled back
        assert s.query(Trade).filter_by(trader_id=jid).count() == 0
    assert market(e, mid).state == State.HUMAN_WINDOW


async def test_create_after_void_opens_a_fresh_attempt():
    e = make(beliefs(0.9, 0.9, 0.9, 0.9))
    rid = e.ensure_repo("acme/widgets")
    first = e.create(rid, bundle())
    e.void(first, "infra")
    second = e.create(rid, bundle())
    assert second != first
    m = market(e, second)
    assert (m.state, m.attempt) == (State.CONTEXT_READY, 2)
    await e.run_agents(second, bundle())  # the retried market really trades
    assert market(e, second).state == State.TRADING


async def test_create_on_a_settled_market_returns_the_same_market():
    e = make(beliefs(0.95, 0.95, 0.95, 0.95))
    mid = await run(e)
    e.close(mid)
    e.gate(mid, None)
    e.settle(mid, YES)
    assert e.create(market(e, mid).repo_id, bundle()) == mid


def test_create_gives_up_after_max_attempts():
    e = make(beliefs(0.9, 0.9, 0.9, 0.9))
    rid = e.ensure_repo("acme/widgets")
    for _ in range(MAX_ATTEMPTS):
        e.void(e.create(rid, bundle()), "infra")
    with pytest.raises(MarketAttemptsExhausted):
        e.create(rid, bundle())


async def test_hung_provider_times_out_and_abstains_while_others_trade():
    e = make(beliefs("hang", 0.9, 0.8, 0.85), belief_timeout_s=0.05)
    mid = await asyncio.wait_for(run(e), 5)
    assert market(e, mid).state == State.TRADING and trades(e, mid)
    with e._sf() as s:
        abstained = list(s.scalars(select(Belief).where(Belief.p_yes.is_(None))))
    assert [(b.trader_id, b.error) for b in abstained if b.escalation == 0] == [(1, "TimeoutError")]
    assert {b.error for b in abstained} == {"TimeoutError"}  # it hangs again in any escalation round


async def test_hung_providers_below_min_traders_void():
    e = make(beliefs("hang", "hang", "hang", 0.9), belief_timeout_s=0.05)
    mid = await asyncio.wait_for(run(e), 5)
    assert market(e, mid).state == State.VOID


async def test_human_trade_db_error_never_voids_the_market(monkeypatch):
    e = make(beliefs(0.6, 0.6, 0.6, 0.6))
    mid = await run(e)
    e.open_human_window(mid)
    jid = add_judge(e)

    def db_down(*_a, **_k):
        raise OperationalError("UPDATE traders", {}, Exception("database is locked"))

    monkeypatch.setattr(e, "_trade", db_down)
    with pytest.raises(OperationalError):
        e.human_trade(mid, jid, "YES", 5.0)
    assert market(e, mid).state == State.HUMAN_WINDOW
    with e._sf() as s:
        assert s.get(Trader, jid).bankroll == 100.0


async def test_gate_is_idempotent_once_gated_settling_or_settled():
    e = make(beliefs(0.75, 0.75, 0.75, 0.75))
    mid = await run(e)
    e.close(mid)
    first = e.gate(mid, None)
    assert (first.state, first.description[:12]) == ("failure", "no consensus")
    assert e.gate(mid, None) == first
    assert e.gate(mid, TargetGateOverrides(human_review=0.8)) == first  # stored, not recomputed
    e.settle(mid, YES)
    assert market(e, mid).state == State.VOID  # rejected -> refunded, and the gate is kept
    assert e.gate(mid, None) == first


async def test_gate_returns_failure_even_if_voiding_also_fails(monkeypatch):
    e = make(beliefs(0.95, 0.95, 0.95, 0.95))
    mid = await run(e)
    e.close(mid)

    def boom(*_a, **_k):
        raise RuntimeError("db down")

    monkeypatch.setattr(e, "_move", boom)
    monkeypatch.setattr(e, "void", boom)
    d = e.gate(mid, None)
    assert (d.state, d.price) == ("failure", None)


# ----------------------------------------------- escalation and refunds (DECISIONS #26-29, R-GATE-1, R-MKT-4)


def events(engine, mid) -> list[Event]:
    with engine._sf() as s:
        return list(s.scalars(select(Event).where(Event.market_id == mid).order_by(Event.id)))


async def test_in_band_price_escalates_then_fails_no_consensus():
    llm = beliefs(0.75, 0.75, 0.75, 0.75)
    e = make(llm)
    mid = await run(e)
    m = market(e, mid)
    assert m.state == State.TRADING and m.escalations == 2
    assert len(llm.calls) == 4 * 3  # one belief per trader per phase
    escalation_events = [
        ev.detail[:16] for ev in events(e, mid) if ev.actor == "engine" and ev.detail[:4] == "esca"
    ]
    assert escalation_events == ["escalation 1/2 (", "escalation 2/2 ("]
    with e._sf() as s:
        assert sorted({b.escalation for b in s.scalars(select(Belief))}) == [0, 1, 2]
    assert m.cost_usd == pytest.approx(12 * 0.001)  # R-TRD-5: escalation calls are costed too
    assert m.latency_ms == 3 * (40 + len("diff_reader"))  # phases run one after another
    p = e.close(mid)
    d = e.gate(mid, None)
    assert (d.state, d.description, d.price) == ("failure", f"no consensus (p={p:.2f})", p)


async def test_escalation_can_resolve_to_auto_merge_and_calls_the_hook():
    llm = Stub(dict.fromkeys(ROLES, 0.75), escalated=dict.fromkeys(ROLES, 0.98))
    e = make(llm)
    seen: list[tuple[int, int, float]] = []

    async def hook(k, n, p):
        seen.append((k, n, p))

    b = bundle()
    mid = e.create(e.ensure_repo(b.repo), b)
    await e.run_agents(mid, b, on_escalation=hook)
    assert [(k, n) for k, n, _ in seen] == [(1, 2)] and 0.6 <= seen[0][2] < 0.9
    assert market(e, mid).escalations == 1  # the price left the band, so no second round
    assert any(t.escalation == 1 for t in trades(e, mid))
    e.close(mid)
    assert e.gate(mid, None).state == "success"


async def test_escalation_shows_peers_rationales_and_the_price_never_their_numbers():
    llm = beliefs(0.75, 0.7, 0.8, 0.72)
    e = make(llm)
    await run(e)
    ctxs = [(name, c) for name, c in llm.calls if c is not None and c.round == 1]
    assert [name for name, _ in ctxs] == list(ROLES)
    for name, c in ctxs:
        assert [pv.trader for pv in c.peers] == [r for r in ROLES if r != name]
        assert [pv.rationale for pv in c.peers] == [f"why {r}" for r in ROLES if r != name]
        assert c.previous_rationale == f"why {name}" and c.previous_p_yes is not None
        assert not hasattr(c.peers[0], "p_yes")
        assert c.max_rounds == 2 and 0.6 <= c.price < 0.9


async def test_no_escalation_outside_the_band():
    for p_yes in (0.95, 0.2):
        llm = beliefs(p_yes, p_yes, p_yes, p_yes)
        e = make(llm)
        mid = await run(e)
        assert market(e, mid).escalations == 0 and len(llm.calls) == 4


async def test_max_escalations_zero_disables_escalation():
    llm = beliefs(0.75, 0.75, 0.75, 0.75)
    e = make(llm, cfg=ServerConfig(market={"max_escalations": 0}))
    mid = await run(e)
    assert market(e, mid).escalations == 0 and len(llm.calls) == 4
    e.close(mid)
    assert e.gate(mid, None).description.startswith("no consensus")


async def test_escalation_band_comes_from_the_target_gate():
    """The band is the base-branch .prm.toml's gate: with B = 0.80 a 0.75 market is simply blocked."""
    strict = parse_target_config(
        '[target]\nname = "t"\nholdouts_repo = "o/h"\n[settle]\ncommand = "run {holdouts}"\n'
        "[gate]\nhuman_review = 0.80\n"
    )
    llm = beliefs(0.75, 0.75, 0.75, 0.75)
    e = make(llm)
    mid = await run(e, bundle(target=strict))
    assert market(e, mid).escalations == 0
    e.close(mid)
    assert e.gate(mid, strict.gate).description.startswith("blocked")


async def test_escalation_with_fewer_than_two_responders_voids():
    e = make(Stub(dict.fromkeys(ROLES, 0.75), escalated={"diff_reader": 0.8, "coverage": None,
                                                           "security": None, "skeptic": None}))  # fmt: skip
    mid = await run(e)
    m = market(e, mid)
    assert m.state == State.VOID and m.gate_description == VOID_DESCRIPTION and m.resolution == "void"
    assert set(bankrolls(e).values()) == {100.0}


async def test_provider_without_escalation_support_abstains_and_fails_closed():
    """A provider still on the old signature can't re-estimate: it abstains, and the market VOIDs."""

    class Legacy:
        async def get_belief(self, spec, bundle):
            return BeliefResult(spec.name, 0.75, "r", [], spec.model, 0, 0, 0, 0.0, None)

    e = make(Legacy())
    mid = await run(e)
    assert market(e, mid).state == State.VOID
    with e._sf() as s:
        assert {b.error for b in s.scalars(select(Belief).where(Belief.escalation == 1))} == {"TypeError"}


async def test_blocked_market_runs_the_unpaid_check_then_refunds_everyone():
    e = make(beliefs(0.2, 0.2, 0.2, 0.2))
    mid = await run(e)
    assert any(v < 100.0 for v in bankrolls(e).values())
    e.close(mid)
    blocked = e.gate(mid, None)
    assert blocked.description.startswith("blocked")
    assert e.settle(mid, NO) == {}
    m = market(e, mid)
    assert (m.state, m.resolution, m.outcome) == (State.VOID, "refunded", "NO")
    assert (m.holdouts_passed, m.holdouts_total) == (3, 5)  # stored for Brier/calibration (R-LG-2)
    assert bankrolls(e) == pytest.approx(dict.fromkeys(ROLES, 100.0))  # nobody profits from a rejection
    assert e.gate(mid, None) == blocked  # the gate text survives the refund
    assert e.status_description(mid) == f"{blocked.description} · refunded · holdouts agree"
    last = events(e, mid)[-1]
    assert (last.from_state, last.to_state) == ("GATED", "VOID") and "unpaid check" in last.detail
    assert e.settle(mid, NO) == {} and bankrolls(e) == pytest.approx(dict.fromkeys(ROLES, 100.0))
    assert e.create(m.repo_id, bundle()) == mid  # a rejection is final for this sha, not retried


async def test_no_consensus_market_is_refunded_too():
    e = make(beliefs(0.75, 0.75, 0.75, 0.75))
    mid = await run(e)
    e.close(mid)
    e.gate(mid, None)
    e.settle(mid, YES)
    assert market(e, mid).resolution == "refunded"
    assert bankrolls(e) == pytest.approx(dict.fromkeys(ROLES, 100.0))
    assert e.status_description(mid).endswith("· refunded · holdouts disagree")


async def test_demo_market_pays_out_even_when_blocked():
    """Judge markets are a game, not a merge: they always settle on the outcome (R-UI-2)."""
    e = make(beliefs(0.95, 0.9, 0.3, 0.2))
    b = bundle(source="demo")
    mid = await run(e, b)
    e.close(mid)
    assert e.gate(mid, None).state == "failure"
    before = bankrolls(e)
    payouts = e.settle(mid, NO)
    assert market(e, mid).state == State.SETTLED and payouts["skeptic"] > 0
    assert bankrolls(e)["skeptic"] == pytest.approx(before["skeptic"] + payouts["skeptic"])
    assert e.status_description(mid).endswith("· settled NO · market right")


@pytest.mark.parametrize(("result", "verdict"), [(YES, "market right"), (NO, "market wrong")])
async def test_status_description_for_a_merged_market(result, verdict):
    e = make(beliefs(0.95, 0.95, 0.95, 0.95))
    mid = await run(e)
    e.close(mid)
    d = e.gate(mid, None)
    assert e.status_description(mid) == d.description
    e.settle(mid, result)
    assert e.status_description(mid) == f"{d.description} · settled {result.outcome} · {verdict}"
    assert market(e, mid).resolution == "paid"
