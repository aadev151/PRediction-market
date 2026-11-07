"""WP A. MarketEngine: create, run_agents, human window, close, gate, settle, void. PLAN §6.8.

Agents only emit numbers; this engine owns every trade, gate, payout and state transition. It fails closed:
an unexpected exception inside a market step VOIDs the market (refunds, gate ``failure``) before it
propagates, and ``gate()`` always returns a decision. Caller mistakes (``IllegalAction``,
``IllegalTransition``, ``TradeRejected``, ``TradeConflict``, ``MarketNotFound``) raise without voiding, and a
human trade never voids a market: its transaction just rolls back.

Gate and settlement (DECISIONS #26-29): ``p >= auto_merge`` merges, ``p < human_review`` (B) blocks, and in
between the agents run escalation rounds while still TRADING (each sees the others' rationales and the price,
re-estimates once, and trades with what is left of its cumulative cap). Still in between at close -> "no
consensus" (failure). Only a merged market (gate ``success``) pays out on its outcome. A rejected one runs the
holdouts as an unpaid check, then goes VOID with every spend refunded and its gate text kept. Demo (judge)
markets always pay out on the outcome: they are a game, not a merge decision.

Concurrency: markets, traders and positions are optimistically locked (``version_id``). A stale write raises
``StaleDataError`` and rolls back the whole transaction; engine steps re-read and retry, human trades turn it
into ``TradeConflict``.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import random
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from dataclasses import asdict
from datetime import datetime
from typing import Protocol, TypeVar

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.orm.exc import StaleDataError

from prm.config import ConfigError, ServerConfig, TargetGateOverrides, TraderSpec
from prm.market import lmsr
from prm.market.state import IllegalAction, IllegalTransition, require_action, transition
from prm.models import Belief, Event, Market, Position, Repo, Trade, Trader, utcnow
from prm.types import (
    BELIEF_MAX,
    BELIEF_MIN,
    TERMINAL_STATES,
    BeliefResult,
    EscalationContext,
    GateDecision,
    PeerView,
    PRBundle,
    SettlementResult,
    Side,
    State,
    TradeResult,
)

log = logging.getLogger(__name__)

VOID_DESCRIPTION = "market void: human review required"
VOID_GATE = GateDecision("failure", VOID_DESCRIPTION, None)
HUMAN_ROUND = 0  # human trades are logged as round 0; agent rounds are 1..rounds
MAX_ATTEMPTS = 3  # markets per (repo, pr, head_sha, source); a VOID one may be retried
STALE_RETRIES = 3
DEFAULT_BELIEF_TIMEOUT_S = 150.0  # the LLM client allows 45 s x up to 3 attempts
_GATED_OR_LATER = frozenset({State.GATED, State.SETTLING, State.SETTLED})
PAID, REFUNDED, VOIDED = "paid", "refunded", "void"  # markets.resolution

T = TypeVar("T")
# Called before each escalation round with (round, max_rounds, price), e.g. to post a pending status.
EscalationHook = Callable[[int, int, float], Awaitable[None]]


class BeliefProvider(Protocol):
    """The A<->B seam. Implementations should never raise; if one does, the trader abstains.

    ``escalation`` is None for the first belief. In an escalation round it carries the price and the other
    traders' rationales; the engine only passes it as a keyword, and only in escalation rounds.
    """

    async def get_belief(
        self, spec: TraderSpec, bundle: PRBundle, escalation: EscalationContext | None = None
    ) -> BeliefResult: ...


class MarketNotFound(LookupError):
    pass


class TradeRejected(ValueError):
    """A human trade the caps or bankroll don't allow (the API maps this to 422)."""


class TradeConflict(RuntimeError):
    """A concurrent update touched the same trader or market; the caller may retry (API: 409)."""


class MarketAttemptsExhausted(RuntimeError):
    """This PR/sha already VOIDed ``MAX_ATTEMPTS`` times. The pipeline fails closed on it."""


_EXPECTED = (IllegalAction, IllegalTransition, TradeRejected, TradeConflict, MarketNotFound)


class MarketEngine:
    def __init__(
        self,
        session_factory: sessionmaker[Session],
        cfg: ServerConfig,
        llm: BeliefProvider,
        clock: Callable[[], datetime] = utcnow,
        rng: random.Random | None = None,
        *,
        belief_timeout_s: float = DEFAULT_BELIEF_TIMEOUT_S,
    ) -> None:
        self._sf = session_factory
        self.cfg = cfg
        self.llm = llm
        self._clock = clock
        self._rng = rng or random.Random()  # noqa: S311 - trade order, not security
        self._belief_timeout_s = belief_timeout_s

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
    def _fail_closed(self, market_id: int, step: str, *, reraise: bool = True) -> Iterator[None]:
        """The one fail-closed policy: void on an unexpected error (even if voiding fails), then re-raise
        unless ``reraise`` is False, in which case the exception is swallowed after logging."""
        try:
            yield
        except _EXPECTED:
            raise
        except (Exception, asyncio.CancelledError) as exc:
            log.exception("market %s: %s failed; voiding", market_id, step)
            try:
                self.void(market_id, f"{step} failed: {type(exc).__name__}")
            except Exception:
                log.exception("market %s: void after %s failure also failed", market_id, step)
            if reraise:
                raise

    @staticmethod
    def _retry_stale(fn: Callable[[], T]) -> T:
        """Run a transaction, re-running it from a fresh read if a concurrent writer made it stale."""
        for attempt in range(STALE_RETRIES):
            try:
                return fn()
            except StaleDataError:
                if attempt == STALE_RETRIES - 1:
                    raise
                log.info("stale write; retrying (%d/%d)", attempt + 1, STALE_RETRIES)
        raise AssertionError("unreachable")  # pragma: no cover

    def _trade(
        self,
        s: Session,
        m: Market,
        trader: Trader,
        belief: float,
        budget: float,
        round_: int,
        escalation: int = 0,
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
                market_id=m.id, trader_id=trader.id, escalation=escalation, round=round_, side=t.side,
                shares=t.shares, cost=t.cost, price_before=p, price_after=p_after, created_at=now,
            )
        )  # fmt: skip
        phase = f"escalation {escalation}, " if escalation else ""
        move = f"{t.side} {t.shares:.4f} shares for {t.cost:.4f} ({p:.4f} -> {p_after:.4f})"
        detail = f"{phase}round {round_}: {move}"
        s.add(Event(market_id=m.id, from_state=m.state, to_state=m.state, actor=trader.name, detail=detail,
                    created_at=now))  # fmt: skip
        return TradeResult(trader.name, t.side, t.shares, t.cost, p, p_after, round_)

    async def _safe_belief(
        self, spec: TraderSpec, bundle: PRBundle, escalation: EscalationContext | None = None
    ) -> BeliefResult:
        try:
            if escalation is None:
                call = self.llm.get_belief(spec, bundle)
            else:
                call = self.llm.get_belief(spec, bundle, escalation=escalation)
            result = await asyncio.wait_for(call, self._belief_timeout_s)
            if not isinstance(result, BeliefResult):
                raise TypeError(f"provider returned {type(result).__name__}, not BeliefResult")
            return result
        except Exception as exc:  # a trader never crashes the market; only the type is kept (no secrets)
            return BeliefResult(
                spec.name, None, "", [], spec.model, 0, 0, 0, 0.0, f"{type(exc).__name__}"[:200]
            )

    # ------------------------------------------------------------------------------------------ lifecycle

    def create(self, repo_id: int, bundle: PRBundle) -> int:
        """CREATED -> CONTEXT_READY. Idempotent on (repo, pr_number, head_sha, source): returns the latest
        non-VOID market for that key; if the latest is VOID, opens attempt + 1 (at most ``MAX_ATTEMPTS``).
        A refunded (rejected) market is final for its sha and is returned as is; only error VOIDs retry."""
        with self._sf.begin() as s:
            attempt = 1
            if bundle.pr_number is not None and bundle.head_sha is not None:
                latest = s.scalar(
                    select(Market).where(
                        Market.repo_id == repo_id, Market.pr_number == bundle.pr_number,
                        Market.head_sha == bundle.head_sha, Market.source == bundle.source,
                    ).order_by(Market.attempt.desc()).limit(1)
                )  # fmt: skip
                if latest is not None:
                    if State(latest.state) != State.VOID or latest.resolution == REFUNDED:
                        return latest.id
                    if latest.attempt >= MAX_ATTEMPTS:
                        raise MarketAttemptsExhausted(
                            f"{bundle.source} PR {bundle.pr_number}@{bundle.head_sha} "
                            f"voided {latest.attempt} times"
                        )
                    attempt = latest.attempt + 1
            mp = self.cfg.market
            qy, qn = lmsr.initial_state(mp.initial_price, mp.b)
            m = Market(
                repo_id=repo_id, source=bundle.source, attempt=attempt, pr_number=bundle.pr_number,
                head_sha=bundle.head_sha, base_sha=bundle.base_sha, state=State.CREATED.value, b=mp.b,
                q_yes=qy, q_no=qn, initial_price=mp.initial_price, diff_text=bundle.diff,
                description_text=bundle.description, created_at=self._clock(),
            )  # fmt: skip
            s.add(m)
            s.flush()
            s.add(Event(market_id=m.id, from_state=None, to_state=State.CREATED.value, actor="engine",
                        detail=f"market created (attempt {attempt})", created_at=self._clock()))  # fmt: skip
            self._agents(s, repo_id)
            self._move(s, m, State.CONTEXT_READY)
            return m.id

    async def run_agents(
        self, market_id: int, bundle: PRBundle, *, on_escalation: EscalationHook | None = None
    ) -> list[BeliefResult]:
        """CONTEXT_READY -> TRADING; parallel beliefs (each bounded by ``belief_timeout_s``); ``rounds``
        rounds of capped trades in random order. Fewer than ``min_traders`` responders -> VOID.

        Then, while the price sits in ``[human_review, auto_merge)`` of the target's gate (``bundle.target``,
        read from the base branch), up to ``max_escalations`` escalation rounds: ``on_escalation`` is awaited,
        every trader re-estimates once with an ``EscalationContext`` and the agents trade ``rounds`` more
        rounds with what is left of their cumulative caps (so R-MKT-3 still holds). An escalation round with
        fewer than ``min_traders`` responders VOIDs too, and so does a hook that raises (fail closed).
        Returns every belief, in order.
        """
        with self._sf.begin() as s:
            self._move(s, self._market(s, market_id), State.TRADING)
        with self._fail_closed(market_id, "run_agents"):
            specs = list(self.cfg.traders)
            beliefs = list(await asyncio.gather(*(self._safe_belief(sp, bundle) for sp in specs)))
            every = list(beliefs)
            if not self._trading_phase(market_id, specs, beliefs, 0):
                return every
            latest = self._responders(specs, beliefs)
            band = self._escalation_band(bundle)
            max_rounds = self.cfg.market.max_escalations
            for k in range(1, max_rounds + 1):
                price = self._retry_stale(lambda k=k: self._begin_escalation(market_id, k, max_rounds, band))
                if price is None:
                    break
                if on_escalation is not None:
                    await on_escalation(k, max_rounds, price)
                ctx = {
                    sp.name: self._escalation_context(sp.name, k, max_rounds, price, latest) for sp in specs
                }
                beliefs = list(
                    await asyncio.gather(*(self._safe_belief(sp, bundle, ctx[sp.name]) for sp in specs))
                )
                every.extend(beliefs)
                if not self._trading_phase(market_id, specs, beliefs, k):
                    break
                latest.update(self._responders(specs, beliefs))
            return every

    def _trading_phase(
        self, market_id: int, specs: list[TraderSpec], beliefs: list[BeliefResult], escalation: int
    ) -> bool:
        """Record and trade one phase. False if the market left TRADING or was voided for too few traders."""
        responders = self._retry_stale(lambda: self._record_and_trade(market_id, specs, beliefs, escalation))
        if responders is None:
            return False
        need = self.cfg.market.min_traders
        if responders < need:
            phase = f" in escalation {escalation}" if escalation else ""
            self.void(market_id, f"only {responders} trader(s) responded{phase}; need {need}")
            return False
        return True

    def _escalation_band(self, bundle: PRBundle) -> tuple[float, float] | None:
        """[B, A) for this target. None (no escalation) if the overrides are unsafe: ``gate()`` voids then."""
        try:
            th = self.cfg.gate_for(bundle.target.gate)
        except ConfigError:
            return None
        return th.human_review, th.auto_merge

    def _begin_escalation(
        self, market_id: int, k: int, max_rounds: int, band: tuple[float, float] | None
    ) -> float | None:
        """If the market is TRADING with its price in the band, log escalation ``k`` and return the price."""
        if band is None:
            return None
        with self._sf.begin() as s:
            m = self._market(s, market_id)
            if State(m.state) != State.TRADING:
                return None
            p = lmsr.price(m.q_yes, m.q_no, m.b)
            if not band[0] <= p < band[1]:
                return None
            m.escalations = k
            detail = f"escalation {k}/{max_rounds} (p={p:.2f})"
            s.add(Event(market_id=m.id, from_state=m.state, to_state=m.state, actor="engine", detail=detail,
                        created_at=self._clock()))  # fmt: skip
            return p

    @staticmethod
    def _responders(specs: list[TraderSpec], beliefs: list[BeliefResult]) -> dict[str, BeliefResult]:
        """Roster name -> belief, for traders that gave a number (keyed by the roster, not the provider)."""
        return {sp.name: br for sp, br in zip(specs, beliefs, strict=True) if br.p_yes is not None}

    @staticmethod
    def _escalation_context(
        name: str, k: int, max_rounds: int, price: float, latest: dict[str, BeliefResult]
    ) -> EscalationContext:
        own = latest.get(name)
        peers = tuple(PeerView(n, br.rationale, list(br.key_risks)) for n, br in latest.items() if n != name)
        return EscalationContext(
            k, max_rounds, price, own.p_yes if own else None, own.rationale if own else "", peers
        )

    def _record_and_trade(
        self, market_id: int, specs: list[TraderSpec], beliefs: list[BeliefResult], escalation: int = 0
    ) -> int | None:
        """Persist every belief, then trade if enough responded. Returns the responder count (None if the
        market left TRADING while beliefs were in flight)."""
        with self._sf.begin() as s:
            m = self._market(s, market_id)
            if State(m.state) != State.TRADING:
                return None
            agents = self._agents(s, m.repo_id)
            for spec, br in zip(specs, beliefs, strict=True):
                s.add(
                    Belief(
                        market_id=m.id, trader_id=agents[spec.name].id, escalation=escalation,
                        p_yes=br.p_yes, rationale=br.rationale, key_risks_json=json.dumps(br.key_risks),
                        model=br.model, input_tokens=br.input_tokens, output_tokens=br.output_tokens,
                        latency_ms=br.latency_ms, cost_usd=br.cost_usd, error=br.error,
                    )
                )  # fmt: skip
            m.cost_usd += sum(br.cost_usd for br in beliefs)
            # beliefs in a phase run in parallel and phases run in sequence: wall time = sum of phase maxima
            m.latency_ms += max((br.latency_ms for br in beliefs), default=0)
            active = [(agents[sp.name], br.p_yes) for sp, br in zip(specs, beliefs, strict=True)
                      if br.p_yes is not None and math.isfinite(br.p_yes)]  # fmt: skip
            if len(active) >= self.cfg.market.min_traders:
                require_action(m, "agent_trade")
                for r in range(1, self.cfg.market.rounds + 1):
                    for trader, p_yes in self._rng.sample(active, k=len(active)):
                        belief = min(max(p_yes, BELIEF_MIN), BELIEF_MAX)
                        self._trade(s, m, trader, belief, math.inf, r, escalation)
            return len(active)

    def open_human_window(self, market_id: int) -> None:
        with self._sf.begin() as s:
            self._move(s, self._market(s, market_id), State.HUMAN_WINDOW)

    def human_trade(self, market_id: int, trader_id: int, side: Side, spend: float) -> TradeResult:
        """Only in HUMAN_WINDOW, only human traders, same caps as agents. Spends exactly ``spend``.

        Never voids the market: on any error the transaction rolls back and the exception propagates
        (a concurrent update becomes ``TradeConflict``).
        """
        try:
            with self._sf.begin() as s:
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
        except (StaleDataError, IntegrityError) as exc:
            raise TradeConflict("a concurrent update touched this trade; retry") from exc

    def close(self, market_id: int) -> float:
        """TRADING | HUMAN_WINDOW -> CLOSED. Returns the closing price."""
        with self._fail_closed(market_id, "close"), self._sf.begin() as s:
            m = self._market(s, market_id)
            self._move(s, m, State.CLOSED)
            m.final_price = lmsr.price(m.q_yes, m.q_no, m.b)
            m.closed_at = self._clock()
            return m.final_price

    def gate(self, market_id: int, target_gate_overrides: TargetGateOverrides | None) -> GateDecision:
        """CLOSED -> GATED: ``success`` "auto-merge OK", or ``failure`` "blocked" / "no consensus". Never
        ``pending``: escalation already happened while TRADING. Idempotent: a gated (or later) market and a
        refunded (rejected) one return their stored decision, any other VOID market the failure decision.
        Any unexpected error VOIDs and still returns ``failure``; it never raises one."""
        with self._fail_closed(market_id, "gate", reraise=False):
            return self._gate(market_id, target_gate_overrides)
        return VOID_GATE

    @staticmethod
    def _stored_gate(m: Market) -> GateDecision:
        return GateDecision(m.gate_state, m.gate_description or "", m.final_price)  # type: ignore[arg-type]

    def _gate(self, market_id: int, overrides: TargetGateOverrides | None) -> GateDecision:
        with self._sf.begin() as s:
            m = self._market(s, market_id)
            state = State(m.state)
            if state == State.VOID:
                return self._stored_gate(m) if m.resolution == REFUNDED else VOID_GATE
            if state in _GATED_OR_LATER:
                return self._stored_gate(m)
            require_action(m, "gate")
            try:
                th = self.cfg.gate_for(overrides)
            except ConfigError:
                th = None
            if th is not None:
                p = m.final_price if m.final_price is not None else lmsr.price(m.q_yes, m.q_no, m.b)
                if p >= th.auto_merge:
                    d = GateDecision("success", f"auto-merge OK (p={p:.2f})", p)
                elif p < th.human_review:
                    d = GateDecision("failure", f"blocked (p={p:.2f})", p)
                else:  # still undecided after the escalation rounds: fail closed (DECISIONS #28)
                    d = GateDecision("failure", f"no consensus (p={p:.2f})", p)
                m.gate_state, m.gate_description, m.final_price = d.state, d.description, p
                self._move(s, m, State.GATED, d.description)
                return d
        self.void(market_id, "unsafe gate overrides")
        return VOID_GATE

    def settle(self, market_id: int, result: SettlementResult) -> dict[str, float]:
        """Apply the oracle's result to a GATED market. ``result.error`` VOIDs (refund, no outcome).

        - Merged (gate ``success``) or a demo market: SETTLING -> payouts on the outcome -> SETTLED.
        - Rejected (blocked / no consensus): the result is stored as an unpaid check, every spend is refunded,
          and the market goes VOID with resolution ``refunded``, keeping its gate (DECISIONS #27, #29).

        Returns the payouts ({} when nothing was paid). Idempotent and race-safe: an already SETTLED market
        (including one a concurrent call just settled) pays nothing and returns the stored payouts; a VOID
        market returns {}.
        """
        with self._fail_closed(market_id, "settle"):
            return self._retry_stale(lambda: self._settle_once(market_id, result))

    @staticmethod
    def _pays_out(m: Market) -> bool:
        """Only merged markets settle on the outcome; judge (demo) markets are a game and always do."""
        return m.gate_state == "success" or m.source == "demo"

    def _settle_once(self, market_id: int, result: SettlementResult) -> dict[str, float]:
        with self._sf.begin() as s:
            m = self._market(s, market_id)
            if State(m.state) == State.SETTLED:
                return self._payouts(s, m)
            if State(m.state) == State.VOID:
                return {}
            require_action(m, "settle")
            if not result.error:
                m.outcome = result.outcome
                m.holdouts_passed, m.holdouts_total = result.holdouts_passed, result.holdouts_total
                m.findings_json = json.dumps([asdict(f) for f in result.findings])
                m.settled_at = self._clock()
                counts = f"{result.holdouts_passed}/{result.holdouts_total}"
                if not self._pays_out(m):
                    self._refund(s, m)
                    m.resolution = REFUNDED
                    detail = f"rejected, refunded; unpaid check: outcome {result.outcome} ({counts})"
                    self._move(s, m, State.VOID, detail)
                    return {}
                self._move(s, m, State.SETTLING, f"outcome {result.outcome} ({counts})")
                require_action(m, "payout")
                for pos in s.scalars(select(Position).where(Position.market_id == m.id)):
                    pos.payout = pos.yes_shares if result.outcome == "YES" else pos.no_shares
                    s.get_one(Trader, pos.trader_id).bankroll += pos.payout
                m.resolution = PAID
                self._move(s, m, State.SETTLED)
                return self._payouts(s, m)
        self.void(market_id, "settlement infrastructure error")
        return {}

    def _payouts(self, s: Session, m: Market) -> dict[str, float]:
        rows = s.execute(
            select(Trader.name, Position.payout)
            .select_from(Position)
            .join(Trader, Trader.id == Position.trader_id)
            .where(Position.market_id == m.id)
        ).all()
        return {name: payout or 0.0 for name, payout in rows}

    def _refund(self, s: Session, m: Market) -> None:
        for pos in s.scalars(select(Position).where(Position.market_id == m.id)):
            s.get_one(Trader, pos.trader_id).bankroll += pos.spent
            pos.payout = pos.spent

    def void(self, market_id: int, reason: str) -> None:
        """Any non-terminal state -> VOID; refund every position's ``spent`` once; gate = failure ("market
        void: human review required"). Terminal markets: no-op."""
        self._retry_stale(lambda: self._void_once(market_id, reason))

    def _void_once(self, market_id: int, reason: str) -> None:
        with self._sf.begin() as s:
            m = self._market(s, market_id)
            if State(m.state) in TERMINAL_STATES:
                return
            self._refund(s, m)
            m.gate_state, m.gate_description, m.resolution = "failure", VOID_DESCRIPTION, VOIDED
            self._move(s, m, State.VOID, reason)
        log.warning("market %s voided: %s", market_id, reason)

    def status_description(self, market_id: int) -> str:
        """The final ``pr-market`` description (R-GATE-3), e.g. "auto-merge OK (p=0.94) · settled YES · market
        right" or "blocked (p=0.21) · refunded · holdouts agree". Before settlement: just the gate text."""
        with self._sf() as s:
            m = self._market(s, market_id)
            gate_text = m.gate_description or ""
            if m.resolution == PAID and m.outcome is not None:
                right = (m.gate_state == "success") == (m.outcome == "YES")
                return f"{gate_text} · settled {m.outcome} · market {'right' if right else 'wrong'}"
            if m.resolution == REFUNDED and m.outcome is not None:
                agree = "agree" if m.outcome == "NO" else "disagree"
                return f"{gate_text} · refunded · holdouts {agree}"
            return gate_text
