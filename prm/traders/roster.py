"""WP B. Map ServerConfig.traders to belief providers (llm / fake / replay). PLAN §6.4.

The engine (WP A) talks to traders through ``prm.market.engine.BeliefProvider``:
``async get_belief(spec, bundle)`` (DECISIONS #34). ``EngineProvider`` adapts any WP B provider to that seam:
it builds the trader's context and delegates. ``make_engine_provider(cfg)`` is what the pipeline, CLI and
league pass to ``MarketEngine``. Every failure becomes an abstention carrying only the exception *type*
(an exception message could carry request data), so the engine's "fewer than ``min_traders`` responded ->
VOID" rule is the only place a market can be stopped.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from prm.config import RuntimeEnv, ServerConfig, TraderSpec
from prm.traders.base import BeliefProvider, abstain
from prm.traders.context import Evidence, build_context, gather_evidence
from prm.traders.fake import FakeProvider
from prm.traders.llm import LLMProvider
from prm.traders.replay import ReplayProvider
from prm.types import BeliefResult, EscalationContext, PRBundle


def make_provider(cfg: ServerConfig, env: RuntimeEnv | None = None) -> BeliefProvider:
    """Replay (``PRM_REPLAY_BELIEFS``, falling back to the fake if ``PRM_FAKE_LLM``) > fake > live LLM."""
    env = env or RuntimeEnv.from_env()
    if env.replay_beliefs is not None:
        return ReplayProvider(env.replay_beliefs, fallback=FakeProvider() if env.fake_llm else None)
    if env.fake_llm:
        return FakeProvider()
    return LLMProvider(cfg)


class EngineProvider:
    """Satisfies ``prm.market.engine.BeliefProvider``. Never raises: every failure abstains."""

    def __init__(self, provider: BeliefProvider) -> None:
        self.provider = provider
        self._cached: tuple[PRBundle, Evidence] | None = None

    def _evidence(self, bundle: PRBundle) -> Evidence:
        # The engine asks every trader about the same bundle object at once: parse the diff once.
        if self._cached is None or self._cached[0] is not bundle:
            self._cached = (bundle, gather_evidence(bundle))
        return self._cached[1]

    async def get_belief(
        self, spec: TraderSpec, bundle: PRBundle, escalation: EscalationContext | None = None
    ) -> BeliefResult:
        """Exactly the engine's ``BeliefProvider.get_belief`` (DECISIONS #39). Round 1: ``get_belief(spec,
        bundle)``. Escalation round: ``get_belief(spec, bundle, escalation=EscalationContext(...))``."""
        try:
            ctx = build_context(spec.name, bundle, self._evidence(bundle), escalation)
        except Exception as exc:  # fail closed: this trader abstains
            return abstain(spec.name, spec.model, f"context error: {type(exc).__name__}")
        try:
            if escalation is None:
                result = await self.provider.belief(spec, ctx, bundle)
            else:
                result = await self.provider.belief(spec, ctx, bundle, escalation=escalation)
        except Exception as exc:  # a trader never raises
            return abstain(spec.name, spec.model, f"provider error: {type(exc).__name__}")
        if not isinstance(result, BeliefResult):
            return abstain(spec.name, spec.model, f"provider returned {type(result).__name__}")
        if result.trader != spec.name:
            return abstain(spec.name, spec.model, "provider answered as another trader")
        return result


def make_engine_provider(cfg: ServerConfig, env: RuntimeEnv | None = None) -> EngineProvider:
    """The provider to hand to ``MarketEngine`` (pipeline, CLI, league)."""
    return EngineProvider(make_provider(cfg, env))


async def gather_beliefs(
    provider: BeliefProvider,
    cfg: ServerConfig,
    bundle: PRBundle,
    traders: Sequence[TraderSpec] | None = None,
) -> list[BeliefResult]:
    """One belief per trader, in roster order, outside the engine (smoke runs, league belief caching)."""
    specs = list(cfg.traders if traders is None else traders)
    adapter = EngineProvider(provider)
    return list(await asyncio.gather(*(adapter.get_belief(s, bundle) for s in specs)))
