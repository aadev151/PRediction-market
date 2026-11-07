from unittest.mock import patch

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from prm.config import TraderSpec
from prm.traders.fake import FAKE_MODEL, FakeProvider, fake_belief
from prm.types import BELIEF_MAX, BELIEF_MIN, TRADER_ROLES, TraderContext
from tests.traders._bundles import BUG_DIFF, GOOD_DIFF, make_bundle


def test_deterministic(bundle):
    assert fake_belief("security", bundle) == fake_belief("security", bundle)
    assert fake_belief("security", bundle).p_yes != fake_belief("diff_reader", bundle).p_yes


def test_bug_diff_crashes_the_price_band(bundle):
    beliefs = {r: fake_belief(r, bundle) for r in TRADER_ROLES}
    assert beliefs["security"].p_yes <= 0.14
    assert max(b.p_yes for b in beliefs.values()) < 0.60  # the LMSR price can't exceed the top belief
    assert "removed_check" in beliefs["security"].key_risks


def test_good_diff_is_confident_enough_to_merge(tmp_path):
    """No human band (DECISIONS #28): every quiet belief must clear the 0.90 merge line by more than EPS."""
    b = make_bundle(tmp_path, diff=GOOD_DIFF)
    beliefs = {r: fake_belief(r, b).p_yes for r in TRADER_ROLES}
    assert all(0.91 <= p <= 0.98 for p in beliefs.values()), beliefs


@settings(suppress_health_check=[HealthCheck.function_scoped_fixture], max_examples=200)
@given(u=st.floats(0.0, 1.0, exclude_max=True), role=st.sampled_from(TRADER_ROLES))
def test_quiet_and_high_signal_ranges_hold_for_every_hash(tmp_path, u, role):
    """fake.py's docstring ranges, for ANY sha256 draw: quiet ≥ 0.91 (merges), high signal < 0.60 (blocks)."""
    with patch("prm.traders.fake._unit", return_value=u):
        quiet = fake_belief(role, make_bundle(tmp_path, diff=GOOD_DIFF)).p_yes
        risky = fake_belief(role, make_bundle(tmp_path, diff=BUG_DIFF)).p_yes
    assert 0.91 <= quiet <= 0.98
    assert risky < 0.60


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
