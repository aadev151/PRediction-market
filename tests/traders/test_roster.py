from pathlib import Path

from prm.config import RuntimeEnv, ServerConfig, TraderSpec
from prm.traders import roster
from prm.traders.base import abstain
from prm.traders.fake import FakeProvider, fake_belief
from prm.traders.llm import LLMProvider
from prm.traders.replay import ReplayProvider
from prm.traders.roster import gather_beliefs, make_provider

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
            raise RuntimeError("boom")
        if spec.name == "skeptic":
            return abstain("someone_else", "m", "x")
        return fake_belief(spec.name, bundle)


async def test_provider_errors_become_abstentions(bundle):
    results = {r.trader: r for r in await gather_beliefs(_Exploding(), CFG, bundle)}
    assert results["security"].abstained and "RuntimeError: boom" in results["security"].error
    assert results["skeptic"].abstained and "answered as" in results["skeptic"].error
    assert results["security"].model == "claude-sonnet-5-5"
    assert not results["diff_reader"].abstained


async def test_context_error_abstains_everyone(bundle, monkeypatch):
    def broken(*a, **k):
        raise ValueError("bad diff")

    monkeypatch.setattr(roster, "build_contexts", broken)
    results = await gather_beliefs(FakeProvider(), CFG, bundle)
    assert len(results) == 4 and all(r.abstained and "context error" in r.error for r in results)
