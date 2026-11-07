from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from prm.config import TraderSpec
from prm.traders.fake import FAKE_MODEL, FakeProvider, fake_belief
from prm.types import BELIEF_MAX, BELIEF_MIN, TRADER_ROLES, TraderContext
from tests.traders._bundles import GOOD_DIFF, make_bundle


def test_deterministic(bundle):
    assert fake_belief("security", bundle) == fake_belief("security", bundle)
    assert fake_belief("security", bundle).p_yes != fake_belief("diff_reader", bundle).p_yes


def test_bug_diff_crashes_the_price_band(bundle):
    beliefs = {r: fake_belief(r, bundle) for r in TRADER_ROLES}
    assert beliefs["security"].p_yes <= 0.14
    assert max(b.p_yes for b in beliefs.values()) < 0.60  # the LMSR price can't exceed the top belief
    assert "removed_check" in beliefs["security"].key_risks


def test_good_diff_is_optimistic(tmp_path):
    b = make_bundle(tmp_path, diff=GOOD_DIFF)
    beliefs = {r: fake_belief(r, b).p_yes for r in TRADER_ROLES}
    assert beliefs["security"] >= 0.55 and beliefs["diff_reader"] >= 0.55
    assert all(p >= 0.35 for p in beliefs.values())


def test_shape_of_a_fake_belief(bundle):
    r = fake_belief("coverage", bundle)
    assert r.model == FAKE_MODEL and r.error is None and r.cost_usd == 0.0
    assert (r.input_tokens, r.output_tokens, r.latency_ms) == (0, 0, 0)


async def test_provider(bundle):
    ctx = TraderContext("skeptic", "s", "u")
    r = await FakeProvider().belief(TraderSpec(name="skeptic", model="m"), ctx, bundle)
    assert r == fake_belief("skeptic", bundle)


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=50)
@given(diff=st.text(max_size=300), role=st.sampled_from(TRADER_ROLES))
def test_always_a_clamped_probability(tmp_path, diff, role):
    p = fake_belief(role, make_bundle(tmp_path, diff=diff)).p_yes
    assert BELIEF_MIN <= p <= BELIEF_MAX
