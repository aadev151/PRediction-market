"""WP A. MarketEngine: create, run_agents, human window, close, gate, settle, void. PLAN §6.8.

Agents only emit numbers; this engine owns every trade, gate, payout and state transition. It fails closed:
an unexpected exception inside a step VOIDs the market (refunds, gate ``failure``) before it propagates.
Precondition errors (``IllegalAction``, ``IllegalTransition``, ``TradeRejected``, ``MarketNotFound``) are the
caller's mistake, not the market's, so they raise without voiding.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import UTC, datetime
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from prm.config import ConfigError, ServerConfig, TargetGateOverrides, TraderSpec
from prm.market import lmsr
from prm.market.state import IllegalAction, IllegalTransition, require_action, transition
from prm.models import Belief, Event, Market, Position, Repo, Trade, Trader
from prm.types import (
    BELIEF_MAX,
    BELIEF_MIN,
    TERMINAL_STATES,
    BeliefResult,
    GateDecision,
    PRBundle,
    SettlementResult,
    Side,
    State,
    TradeResult,
)

log = logging.getLogger(__name__)

VOID_DESCRIPTION = "market void: human review required"
HUMAN_ROUND = 0  # human trades are logged as round 0; agent rounds are 1..rounds


class BeliefProvider(Protocol):
    """The A<->B seam. Implementations should never raise; if one does, the trader abstains."""

    async def get_belief(self, spec: TraderSpec, bundle: PRBundle) -> BeliefResult: ...


class MarketNotFound(LookupError):
    pass


class TradeRejected(ValueError):
    """A human trade the caps or bankroll don't allow (the API maps this to 422)."""


_EXPECTED = (IllegalAction, IllegalTransition, TradeRejected, MarketNotFound)


def _utcnow() -> datetime:
    return datetime.now(UTC)


class MarketEngine:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        cfg: ServerConfig,
        llm: BeliefProvider,
        clock: Callable[[], datetime] = _utcnow,
        rng: random.Random | None = None,
    ) -> None:
        self._sf = session_factory
        self.cfg = cfg
        self.llm = llm
        self._clock = clock
        self._rng = rng or random.Random()  # noqa: S311 - trade order, not security

    # ------------------------------------------------------------------------------------------ helpers

    def ensure_repo(self, full_name: str) -> int:
        """Get or create a repo and its roster of agent traders (bankrolls are per repo)."""
        with self._sf.begin() as s:
            repo = s.scalar(select(Repo).where(Repo.full_name == full_name))
            if repo is None:
                repo = Repo(full_name=full_name, created_at=self._clock())
                s.add(repo)
                s.flush()
            self._agents(s, repo.id)
            return repo.id

    def _agents(self, s: Session, repo_id: int) -> dict[str, Trader]:
        rows = s.scalars(select(Trader).where(Trader.repo_id == repo_id, Trader.kind == "agent"))
        agents = {t.name: t for t in rows}
        for spec in self.cfg.traders:
            if spec.name not in agents:
                t = Trader(
                    repo_id=repo_id, name=spec.name, kind="agent", model=spec.model,
                    bankroll=self.cfg.market.start_bankroll, created_at=self._clock(),
                )  # fmt: skip
                s.add(t)
                agents[spec.name] = t
        s.flush()
        return agents

    def _market(self, s: Session, market_id: int) -> Market:
        m = s.get(Market, market_id)
        if m is None:
            raise MarketNotFound(market_id)
        return m

    def _move(self, s: Session, m: Market, to: State, detail: str = "") -> None:
        transition(s, m, to, actor="engine", detail=detail, at=self._clock())

    def _position(self, s: Session, m: Market, trader: Trader) -> Position:
        pos = s.get(Position, (m.id, trader.id))
        if pos is None:
            mp = self.cfg.market
            cap = max(0.0, min(mp.max_spend_fraction * trader.bankroll, mp.max_spend_abs))
            pos = Position(
                market_id=m.id, trader_id=trader.id, yes_shares=0.0, no_shares=0.0, spent=0.0, cap=cap
            )
            s.add(pos)
        return pos

    @contextmanager
    def _fail_closed(self, market_id: int, step: str) -> Iterator[None]:
        try:
            yield
        except _EXPECTED:
            raise
        except (Exception, asyncio.CancelledError) as exc:
            log.exception("market %s: %s failed; voiding", market_id, step)
            self.void(market_id, f"{step} failed: {type(exc).__name__}")
            raise

    def _trade(
        self, s: Session, m: Market, trader: Trader, belief: float, budget: float, round_: int
    ) -> TradeResult | None:
        pos = self._position(s, m, trader)
        budget = min(budget, pos.cap - pos.spent, trader.bankroll)
        p = lmsr.price(m.q_yes, m.q_no, m.b)
        t = lmsr.budget_trade(p, belief, budget, m.b)
        if t is None:
            return None
        if t.side == "YES":
            m.q_yes += t.shares
            pos.yes_shares += t.shares
        else:
            m.q_no += t.shares
            pos.no_shares += t.shares
        pos.spent += t.cost
        trader.bankroll -= t.cost
        p_after = lmsr.price(m.q_yes, m.q_no, m.b)
        now = self._clock()
        s.add(
            Trade(
                market_id=m.id, trader_id=trader.id, round=round_, side=t.side, shares=t.shares,
                cost=t.cost, price_before=p, price_after=p_after, created_at=now,
            )
        )  # fmt: skip
        detail = f"round {round_}: {t.side} {t.shares:.4f} shares for {t.cost:.4f} ({p:.4f} -> {p_after:.4f})"
        s.add(Event(market_id=m.id, from_state=m.state, to_state=m.state, actor=trader.name, detail=detail,
                    created_at=now))  # fmt: skip
        return TradeResult(trader.name, t.side, t.shares, t.cost, p, p_after, round_)

    async def _safe_belief(self, spec: TraderSpec, bundle: PRBundle) -> BeliefResult:
        try:
            result = await self.llm.get_belief(spec, bundle)
            if not isinstance(result, BeliefResult):
                raise TypeError(f"provider returned {type(result).__name__}, not BeliefResult")
            return result
        except Exception as exc:  # a trader never crashes the market; only the type is kept (no secrets)
            return BeliefResult(
                spec.name, None, "", [], spec.model, 0, 0, 0, 0.0, f"{type(exc).__name__}"[:200]
            )

    # ------------------------------------------------------------------------------------------ lifecycle

    def create(self, repo_id: int, bundle: PRBundle) -> int:
        """CREATED -> CONTEXT_READY. Idempotent on (repo, pr_number, head_sha, source)."""
        with self._sf.begin() as s:
            if bundle.pr_number is not None and bundle.head_sha is not None:
                existing = s.scalar(
                    select(Market.id).where(
                        Market.repo_id == repo_id, Market.pr_number == bundle.pr_number,
                        Market.head_sha == bundle.head_sha, Market.source == bundle.source,
                    )
                )  # fmt: skip
                if existing is not None:
                    return existing
            mp = self.cfg.market
            qy, qn = lmsr.initial_state(mp.initial_price, mp.b)
            m = Market(
                repo_id=repo_id, source=bundle.source, pr_number=bundle.pr_number, head_sha=bundle.head_sha,
                base_sha=bundle.base_sha, state=State.CREATED.value, b=mp.b, q_yes=qy, q_no=qn,
                initial_price=mp.initial_price, diff_text=bundle.diff, description_text=bundle.description,
                created_at=self._clock(),
            )  # fmt: skip
            s.add(m)
            s.flush()
            s.add(Event(market_id=m.id, from_state=None, to_state=State.CREATED.value, actor="engine",
                        detail="market created", created_at=self._clock()))  # fmt: skip
            self._agents(s, repo_id)
            self._move(s, m, State.CONTEXT_READY)
            return m.id

    async def run_agents(self, market_id: int, bundle: PRBundle) -> list[BeliefResult]:
        """CONTEXT_READY -> TRADING; parallel beliefs; ``rounds`` rounds of capped trades in random order."""
        with self._sf.begin() as s:
            self._move(s, self._market(s, market_id), State.TRADING)
        with self._fail_closed(market_id, "run_agents"):
            specs = list(self.cfg.traders)
            beliefs = list(await asyncio.gather(*(self._safe_belief(sp, bundle) for sp in specs)))
            with self._sf.begin() as s:
                m = self._market(s, market_id)
                if State(m.state) != State.TRADING:
                    return beliefs  # voided while we waited
                agents = self._agents(s, m.repo_id)
                for spec, br in zip(specs, beliefs, strict=True):
                    s.add(
                        Belief(
                            market_id=m.id, trader_id=agents[spec.name].id, p_yes=br.p_yes,
                            rationale=br.rationale, key_risks_json=json.dumps(br.key_risks), model=br.model,
                            input_tokens=br.input_tokens, output_tokens=br.output_tokens,
                            latency_ms=br.latency_ms, cost_usd=br.cost_usd, error=br.error,
                        )
                    )  # fmt: skip
                m.cost_usd += sum(br.cost_usd for br in beliefs)
                m.latency_ms = max((br.latency_ms for br in beliefs), default=0)
                active = [(agents[sp.name], br.p_yes) for sp, br in zip(specs, beliefs, strict=True)
                          if br.p_yes is not None and math.isfinite(br.p_yes)]  # fmt: skip
                if len(active) >= self.cfg.market.min_traders:
                    require_action(m, "agent_trade")
                    for r in range(1, self.cfg.market.rounds + 1):
                        for trader, p_yes in self._rng.sample(active, k=len(active)):
                            belief = min(max(p_yes, BELIEF_MIN), BELIEF_MAX)
                            self._trade(s, m, trader, belief, math.inf, r)
                    return beliefs
            n = len(active)
            self.void(market_id, f"only {n} trader(s) responded; need {self.cfg.market.min_traders}")
            return beliefs

    def open_human_window(self, market_id: int) -> None:
        with self._sf.begin() as s:
            self._move(s, self._market(s, market_id), State.HUMAN_WINDOW)

    def human_trade(self, market_id: int, trader_id: int, side: Side, spend: float) -> TradeResult:
        """Only in HUMAN_WINDOW, only human traders, same caps as agents. Spends exactly ``spend``."""
        with self._fail_closed(market_id, "human_trade"), self._sf.begin() as s:
            m = self._market(s, market_id)
            require_action(m, "human_trade")
            trader = s.get(Trader, trader_id)
            if trader is None or trader.kind != "human":
                raise IllegalAction(f"trader {trader_id} is not a human trader")
            if side not in ("YES", "NO"):
                raise TradeRejected(f"side must be YES or NO, got {side!r}")
            pos = self._position(s, m, trader)
            remaining = min(pos.cap - pos.spent, trader.bankroll)
            if not (math.isfinite(spend) and 0 < spend <= remaining + 1e-9):
                raise TradeRejected(f"spend must be in (0, {remaining:.2f}]")
            result = self._trade(s, m, trader, 1.0 if side == "YES" else 0.0, spend, HUMAN_ROUND)
            if result is None:
                raise TradeRejected("price is already at its limit")
            return result

    def close(self, market_id: int) -> float:
        """TRADING | HUMAN_WINDOW -> CLOSED. Returns the closing price."""
        with self._fail_closed(market_id, "close"), self._sf.begin() as s:
            m = self._market(s, market_id)
            self._move(s, m, State.CLOSED)
            m.final_price = lmsr.price(m.q_yes, m.q_no, m.b)
            m.closed_at = self._clock()
            return m.final_price

    def gate(self, market_id: int, target_gate_overrides: TargetGateOverrides | None) -> GateDecision:
        """CLOSED -> GATED. A VOID market (or any failure here) always gates ``failure``."""
        void_decision = GateDecision("failure", VOID_DESCRIPTION, None)
        try:
            with self._sf.begin() as s:
                m = self._market(s, market_id)
                if State(m.state) == State.VOID:
                    return void_decision
                require_action(m, "gate")
                try:
                    th = self.cfg.gate_for(target_gate_overrides)
                except ConfigError:
                    reason = "unsafe gate overrides"
                else:
                    p = m.final_price if m.final_price is not None else lmsr.price(m.q_yes, m.q_no, m.b)
                    if p >= th.auto_merge:
                        d = GateDecision("success", f"auto-merge OK (p={p:.2f})", p)
                    elif p >= th.human_review:
                        d = GateDecision("pending", f"needs 1 human review (p={p:.2f})", p)
                    else:
                        d = GateDecision("failure", f"blocked (p={p:.2f})", p)
                    m.gate_state, m.gate_description = d.state, d.description
                    self._move(s, m, State.GATED, d.description)
                    return d
        except _EXPECTED:
            raise
        except Exception as exc:
            log.exception("market %s: gate failed; voiding", market_id)
            reason = f"gate failed: {type(exc).__name__}"
        self.void(market_id, reason)
        return void_decision

    def settle(self, market_id: int, result: SettlementResult) -> dict[str, float]:
        """GATED -> SETTLING -> payouts -> SETTLED. ``result.error`` VOIDs.

        Idempotent: settling an already SETTLED market pays nothing and returns the stored payouts; a VOID
        market returns {}.
        """
        with self._sf.begin() as s:
            m = self._market(s, market_id)
            if State(m.state) == State.SETTLED:
                return self._payouts(s, m)
            if State(m.state) == State.VOID:
                return {}
            require_action(m, "settle")
        if result.error:
            self.void(market_id, "settlement infrastructure error")
            return {}
        with self._fail_closed(market_id, "settle"), self._sf.begin() as s:
            m = self._market(s, market_id)
            self._move(s, m, State.SETTLING, f"outcome {result.outcome}")
            m.outcome = result.outcome
            m.holdouts_passed, m.holdouts_total = result.holdouts_passed, result.holdouts_total
            m.findings_json = json.dumps([asdict(f) for f in result.findings])
            require_action(m, "payout")
            for pos in s.scalars(select(Position).where(Position.market_id == m.id)):
                pos.payout = pos.yes_shares if result.outcome == "YES" else pos.no_shares
                s.get_one(Trader, pos.trader_id).bankroll += pos.payout
            m.settled_at = self._clock()
            self._move(s, m, State.SETTLED)
            return self._payouts(s, m)

    def _payouts(self, s: Session, m: Market) -> dict[str, float]:
        rows = s.execute(
            select(Trader.name, Position.payout)
            .select_from(Position)
            .join(Trader, Trader.id == Position.trader_id)
            .where(Position.market_id == m.id)
        ).all()
        return {name: payout or 0.0 for name, payout in rows}

    def void(self, market_id: int, reason: str) -> None:
        """Any non-terminal state -> VOID; refund every position's ``spent`` once. Terminal markets: no-op."""
        with self._sf.begin() as s:
            m = self._market(s, market_id)
            if State(m.state) in TERMINAL_STATES:
                return
            for pos in s.scalars(select(Position).where(Position.market_id == m.id)):
                s.get_one(Trader, pos.trader_id).bankroll += pos.spent
                pos.payout = pos.spent
            m.gate_state, m.gate_description = "failure", VOID_DESCRIPTION
            self._move(s, m, State.VOID, reason)
            log.warning("market %s voided: %s", market_id, reason)
