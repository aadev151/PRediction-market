from prm.config import TraderSpec
from prm.traders.base import abstain
from prm.traders.fake import FakeProvider, fake_belief
from prm.traders.replay import ReplayProvider, append_beliefs, belief_key, load_beliefs
from prm.types import BeliefResult, TraderContext

SPEC = TraderSpec(name="security", model="claude-sonnet-5-5")
CTX = TraderContext("security", "s", "u")


def _belief(p: float = 0.3) -> BeliefResult:
    return BeliefResult("security", p, "why", ["r1"], "claude-sonnet-5-5", 100, 20, 900, 0.0004, None)


def test_roundtrip(tmp_path, bundle):
    path = tmp_path / "cache" / "beliefs.jsonl"
    append_beliefs(path, bundle.diff, [_belief(), abstain("coverage", "m", "timeout")])
    table, skipped = load_beliefs(path)
    assert skipped == 0
    assert table[belief_key("security", bundle.diff)] == _belief()
    assert table[belief_key("coverage", bundle.diff)].abstained


def test_last_line_wins_and_bad_lines_are_skipped(tmp_path, bundle):
    path = tmp_path / "b.jsonl"
    append_beliefs(path, bundle.diff, [_belief(0.3), _belief(0.4)])
    with path.open("a") as fh:
        fh.write("not json\n\n")
        fh.write('{"trader": "x", "diff_sha256": "y", "p_yes": 1.5}\n')  # out of range
        fh.write('{"trader": "x", "diff_sha256": "y", "p_yes": null}\n')  # abstain without an error
    table, skipped = load_beliefs(path)
    assert skipped == 3 and len(table) == 1
    assert table[belief_key("security", bundle.diff)].p_yes == 0.4


async def test_provider_hit_miss_and_fallback(tmp_path, bundle):
    path = tmp_path / "b.jsonl"
    append_beliefs(path, bundle.diff, [_belief()])
    assert await ReplayProvider(path).belief(SPEC, CTX, bundle) == _belief()
    other = TraderSpec(name="skeptic", model="m")
    miss = await ReplayProvider(path).belief(other, CTX, bundle)
    assert miss.abstained and "no replayed belief" in miss.error
    fb = await ReplayProvider(path, fallback=FakeProvider()).belief(other, CTX, bundle)
    assert fb == fake_belief("skeptic", bundle)


async def test_missing_file_abstains(tmp_path, bundle):
    r = await ReplayProvider(tmp_path / "nope.jsonl").belief(SPEC, CTX, bundle)
    assert r.abstained and "cannot read replay file" in r.error
