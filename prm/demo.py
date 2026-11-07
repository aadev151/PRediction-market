"""WP J. Judge (demo) markets: a random small league mutant, the agents trade, then judges bet. R-UI-2.

``open()`` draws a mutant whose diff changes fewer than ``MAX_DIFF_LINES`` lines from a pool generated once
(seeded, cached under ``$PRM_CACHE/demo/<target>``) from the local target named by ``PRM_DEMO_TARGET``, and
creates a ``demo`` market. ``run_agents()`` lets the agents trade, then opens the HUMAN_WINDOW.
``close_and_settle()`` closes, gates, runs the target's hidden tests on the mutant (the league oracle,
DECISIONS #69) and pays out on the outcome: demo markets always pay out (DECISIONS #41).

The market stores ``head_sha = sha256(diff)[:40]``, so a restarted server finds the mutant again in the same
deterministic pool. The mutant's private label never leaves this module; settlement keeps counts only.
Fail closed: any error after the market exists VOIDs it (refunds every judge and agent).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import random
import threading
from collections.abc import Callable
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from prm.config import ConfigError, RuntimeEnv, ServerConfig, TargetConfig, load_target_config
from prm.league.mutate import Mutant, generate_mutants
from prm.league.oracle import run_tests
from prm.market.engine import MarketEngine
from prm.models import Market
from prm.types import TERMINAL_STATES, PRBundle, SettlementResult, State

log = logging.getLogger(__name__)

DEMO_TARGET_ENV = "PRM_DEMO_TARGET"
MAX_DIFF_LINES = 40  # changed (+/-) lines, PLAN §4
POOL_SIZE = 40
POOL_BUG_FRACTION = 0.5
POOL_SEED_OFFSET = 1000  # a different draw from the league's own mutants
Oracle = Callable[[Path, TargetConfig, Path], SettlementResult]


class DemoUnavailable(RuntimeError):
    """No usable demo target (unset, unreadable, or without local hidden tests). API: 503."""


class DemoConflict(RuntimeError):
    """The request doesn't fit the market's state (another judge market is open...). API: 409."""


def changed_lines(diff: str) -> int:
    return sum(1 for line in diff.splitlines() if line[:1] in "+-" and not line.startswith(("+++", "---")))


def diff_sha(diff: str) -> str:
    return hashlib.sha256(diff.encode("utf-8")).hexdigest()[:40]


class JudgeDemo:
    """Owns the demo pool and the demo market lifecycle around a ``MarketEngine`` that can run traders."""

    def __init__(
        self,
        engine: MarketEngine,
        session_factory: sessionmaker[Session],
        env: RuntimeEnv,
        *,
        target_dir: Path | None = None,
        pool_size: int = POOL_SIZE,
        rng: random.Random | None = None,
        oracle: Oracle = run_tests,
    ) -> None:
        self.engine = engine
        self._sf = session_factory
        self.env = env
        self._target_dir = target_dir
        self._pool_size = pool_size
        self._rng = rng or random.SystemRandom()
        self._oracle = oracle
        self._loaded: tuple[Path, TargetConfig, list[Mutant]] | None = None
        self._lock = threading.Lock()

    @classmethod
    def from_env(
        cls, session_factory: sessionmaker[Session], cfg: ServerConfig, env: RuntimeEnv
    ) -> JudgeDemo:
        from prm.traders.roster import make_engine_provider

        engine = MarketEngine(session_factory, cfg, make_engine_provider(cfg, env))
        return cls(engine, session_factory, env)

    # -------------------------------------------------------------------------------------------- pool

    def _load(self) -> tuple[Path, TargetConfig, list[Mutant]]:
        if self._loaded is not None:
            return self._loaded
        raw = self._target_dir or os.environ.get(DEMO_TARGET_ENV, "")
        if not raw:
            raise DemoUnavailable(f"{DEMO_TARGET_ENV} is not set")
        root = Path(raw).resolve()
        try:
            target = load_target_config(root)
        except ConfigError as exc:
            raise DemoUnavailable("demo target config is unreadable") from exc
        tests = target.holdouts_dir(root)
        if tests is None or not tests.is_dir():
            raise DemoUnavailable("demo target has no local hidden tests")
        out = Path(self.env.cache_dir).resolve() / "demo" / target.target.name
        mutants = generate_mutants(
            root, out, n=self._pool_size, seed=self.engine.cfg.league.seed + POOL_SEED_OFFSET,
            bug_fraction=POOL_BUG_FRACTION, ctx=target.context,
        )  # fmt: skip
        small = [m for m in mutants if 0 < changed_lines(m.diff) < MAX_DIFF_LINES]
        if not small:
            raise DemoUnavailable("no small mutants in the demo pool")
        self._loaded = (root, target, small)
        log.info("demo pool: %d small mutants of %d", len(small), len(mutants))
        return self._loaded

    def repo_name(self) -> str:
        return f"demo/{self._load()[1].target.name}"

    def _bundle(self, m: Mutant) -> PRBundle:
        root, target, _ = self._load()
        return PRBundle(
            "demo", self.repo_name(), None, diff_sha(m.diff), None, m.diff, m.description, root, m.head_dir,
            target,
        )  # fmt: skip

    # ------------------------------------------------------------------------------------------ lifecycle

    def current(self) -> int | None:
        """The newest demo market's id (any state), or None."""
        with self._sf() as s:
            return s.scalar(select(Market.id).where(Market.source == "demo").order_by(Market.id.desc()))

    def open(self) -> tuple[int, PRBundle]:
        """Draw a small mutant and create its market (CONTEXT_READY). One open judge market at a time."""
        with self._lock:
            open_id = self._open_market()
            if open_id is not None:
                raise DemoConflict(f"judge market {open_id} is still open")
            _, _, pool = self._load()
            bundle = self._bundle(self._rng.choice(pool))
            return self.engine.create(self.engine.ensure_repo(bundle.repo), bundle), bundle

    def _open_market(self) -> int | None:
        with self._sf() as s:
            rows = s.execute(select(Market.id, Market.state).where(Market.source == "demo")).all()
        for market_id, state in rows:
            if State(state) not in TERMINAL_STATES:
                return market_id
        return None

    async def run_agents(self, market_id: int, bundle: PRBundle) -> None:
        """Agents trade (escalations included), then HUMAN_WINDOW. Never raises: errors VOID the market."""
        try:
            await self.engine.run_agents(market_id, bundle)
            if self._state(market_id) == State.TRADING:
                self.engine.open_human_window(market_id)
        except Exception as exc:
            log.error("demo market %s: agents failed (%s); voiding", market_id, type(exc).__name__)
            self.engine.void(market_id, f"demo agents failed: {type(exc).__name__}")

    async def close_and_settle(self, market_id: int) -> dict[str, float]:
        """HUMAN_WINDOW -> CLOSED -> GATED -> hidden tests on the mutant -> SETTLED. Returns the payouts."""
        with self._sf() as s:
            m = s.get(Market, market_id)
            if m is None or m.source != "demo":
                raise DemoConflict("not a judge market")
            if State(m.state) != State.HUMAN_WINDOW:
                raise DemoConflict(f"judge market is {m.state}, not HUMAN_WINDOW")
            head_sha = m.head_sha
        try:
            root, target, pool = self._load()
            mutant = next((x for x in pool if diff_sha(x.diff) == head_sha), None)
            if mutant is None:
                raise LookupError("the market's mutant is no longer in the demo pool")
            self.engine.close(market_id)
            self.engine.gate(market_id, target.gate)
            result = await asyncio.to_thread(self._oracle, mutant.head_dir, target, root)
            return self.engine.settle(market_id, result)
        except Exception as exc:
            log.error("demo market %s: settle failed (%s); voiding", market_id, type(exc).__name__)
            self.engine.void(market_id, f"demo settlement failed: {type(exc).__name__}")
            return {}

    def _state(self, market_id: int) -> State:
        with self._sf() as s:
            m = s.get(Market, market_id)
            return State(m.state) if m is not None else State.VOID
