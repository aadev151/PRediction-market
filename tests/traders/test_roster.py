from pathlib import Path

from prm.config import RuntimeEnv, ServerConfig, TargetGateOverrides, TraderSpec
from prm.db import make_session_factory
from prm.market.engine import BeliefProvider, MarketEngine
from prm.traders import roster
from prm.traders.base import abstain
from prm.traders.fake import FakeProvider, fake_belief
from prm.traders.llm import LLMProvider
from prm.traders.replay import ReplayProvider
from prm.traders.roster import EngineProvider, gather_beliefs, make_engine_provider, make_provider

CFG = ServerConfig()


def test_make_provider_precedence():
    assert isinstance(make_provider(CFG, RuntimeEnv(fake_llm=True)), FakeProvider)
    replay = make_provider(CFG, RuntimeEnv(fake_llm=True, replay_beliefs=Path("x.jsonl")))
    assert isinstance(replay, ReplayProvider) and isinstance(replay.fallback, FakeProvider)
    strict = make_provider(CFG, RuntimeEnv(replay_beliefs=Path("x.jsonl")))
    assert isinstance(strict, ReplayProvider) and strict.fallback is None


def test_make_provider_defaults_to_live_llm_without_touching_the_network():
    provider = make_provider(CFG, RuntimeEnv())
    assert isinstance(provider, LLMProvider) and provider._client is None  # client is created lazily


async def test_gather_with_fake(bundle):
    results = await gather_beliefs(FakeProvider(), CFG, bundle)
    assert [r.trader for r in results] == ["diff_reader", "coverage", "security", "skeptic"]
    assert results == [fake_belief(r.trader, bundle) for r in results]


async def test_gather_subset(bundle):
    only = [TraderSpec(name="security", model="claude-sonnet-5-5")]
    assert [r.trader for r in await gather_beliefs(FakeProvider(), CFG, bundle, only)] == ["security"]


class _Exploding:
    async def belief(self, spec, ctx, bundle):
        if spec.name == "security":
            raise RuntimeError("boom: SECRET_DIFF_TEXT")
        if spec.name == "skeptic":
            return abstain("someone_else", "m", "x")
        if spec.name == "coverage":
            return 0.7  # not a BeliefResult
        return fake_belief(spec.name, bundle)


async def test_provider_errors_become_type_only_abstentions(bundle):
    results = {r.trader: r for r in await gather_beliefs(_Exploding(), CFG, bundle)}
    assert results["security"].abstained and results["security"].error == "provider error: RuntimeError"
    assert results["skeptic"].abstained and "another trader" in results["skeptic"].error
    assert results["coverage"].abstained and results["coverage"].error == "provider returned float"
    assert results["security"].model == "claude-sonnet-5-5"
    assert not results["diff_reader"].abstained
    assert not any("SECRET_DIFF_TEXT" in (r.error or "") for r in results.values())


async def test_context_error_abstains_everyone(bundle, monkeypatch):
    def broken(*a, **k):
        raise ValueError("bad diff")

    monkeypatch.setattr(roster, "gather_evidence", broken)
    results = await gather_beliefs(FakeProvider(), CFG, bundle)
    assert len(results) == 4 and all(r.error == "context error: ValueError" for r in results)


def test_engine_provider_satisfies_the_engine_protocol():
    provider = make_engine_provider(CFG, RuntimeEnv(fake_llm=True))
    assert isinstance(provider, EngineProvider) and isinstance(provider.provider, FakeProvider)
    typed: BeliefProvider = provider  # structural typing: MarketEngine accepts it
    assert callable(typed.get_belief)


async def test_engine_provider_parses_the_diff_once_per_bundle(bundle, monkeypatch):
    calls = []
    real = roster.gather_evidence
    monkeypatch.setattr(roster, "gather_evidence", lambda b: calls.append(b) or real(b))
    adapter = EngineProvider(FakeProvider())
    for spec in CFG.traders:
        await adapter.get_belief(spec, bundle)
    assert len(calls) == 1


async def test_engine_runs_a_market_through_the_adapter(bundle):
    engine = MarketEngine(make_session_factory("sqlite://"), CFG, EngineProvider(FakeProvider()))
    mid = engine.create(engine.ensure_repo(bundle.repo), bundle)
    beliefs = await engine.run_agents(mid, bundle)
    assert beliefs == [fake_belief(s.name, bundle) for s in CFG.traders]
    engine.close(mid)
    assert engine.gate(mid, TargetGateOverrides()).state in ("success", "pending", "failure")
