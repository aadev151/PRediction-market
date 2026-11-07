"""WP B. Map ServerConfig.traders to belief providers (llm / fake / replay). PLAN §6.4.

``gather_beliefs`` is what the engine calls: it builds the four contexts once, asks every trader in
parallel, and always returns one ``BeliefResult`` per trader. Any failure becomes an abstention, so the
engine's "fewer than ``min_traders`` responded → VOID" rule is the only place a market can be stopped.
"""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from prm.config import ConfigError, RuntimeEnv, ServerConfig, TraderSpec
from prm.traders.base import BeliefProvider, abstain
from prm.traders.context import build_contexts
from prm.traders.fake import FakeProvider
from prm.traders.replay import ReplayProvider
from prm.types import BeliefResult, PRBundle


def make_provider(cfg: ServerConfig, env: RuntimeEnv | None = None) -> BeliefProvider:
    """Replay (``PRM_REPLAY_BELIEFS``, falling back to the fake if ``PRM_FAKE_LLM``) > fake > live LLM."""
    env = env or RuntimeEnv.from_env()
    if env.replay_beliefs is not None:
        return ReplayProvider(env.replay_beliefs, fallback=FakeProvider() if env.fake_llm else None)
    if env.fake_llm:
        return FakeProvider()
    raise ConfigError("live LLM traders are not wired yet; set PRM_FAKE_LLM=1 or PRM_REPLAY_BELIEFS")


async def gather_beliefs(
    provider: BeliefProvider,
    cfg: ServerConfig,
    bundle: PRBundle,
    traders: Sequence[TraderSpec] | None = None,
) -> list[BeliefResult]:
    specs = list(cfg.traders if traders is None else traders)
    try:
        contexts = build_contexts(bundle, [s.name for s in specs])
    except Exception as exc:  # fail closed: every trader abstains, the engine voids the market
        return [abstain(s.name, s.model, f"context error: {type(exc).__name__}") for s in specs]

    async def one(spec: TraderSpec) -> BeliefResult:
        try:
            result = await provider.belief(spec, contexts[spec.name], bundle)
        except Exception as exc:  # a trader never raises
            return abstain(spec.name, spec.model, f"provider error: {type(exc).__name__}: {exc}")
        if result.trader != spec.name:
            return abstain(spec.name, spec.model, f"provider answered as {result.trader!r}")
        return result

    return list(await asyncio.gather(*(one(s) for s in specs)))
