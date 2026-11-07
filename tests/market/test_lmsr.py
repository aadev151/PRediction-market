"""LMSR property tests (PLAN §6.3, R-MKT-1, R-MKT-3). Hypothesis is mandatory here."""

import math

import pytest
from hypothesis import assume, given
from hypothesis import strategies as st

from prm.config import solo_yes_cost
from prm.market import lmsr

B = st.floats(min_value=1.0, max_value=200.0)
Q = st.floats(min_value=-500.0, max_value=500.0)
DQ = st.floats(min_value=0.01, max_value=200.0)
P = st.floats(min_value=0.01, max_value=0.99)
BELIEF = st.floats(min_value=0.0, max_value=1.0)
SPEND = st.floats(min_value=0.0, max_value=1000.0)


def close(a: float, b: float, tol: float = 1e-9) -> bool:
    return math.isclose(a, b, rel_tol=tol, abs_tol=tol)


@given(Q, Q, B)
def test_price_is_strictly_between_0_and_1(qy, qn, b):
    assert 0.0 < lmsr.price(qy, qn, b) < 1.0


def test_price_never_hits_0_or_1_even_for_extreme_states():
    assert 0.0 < lmsr.price(1e6, 0.0, 1.0) < 1.0
    assert 0.0 < lmsr.price(0.0, 1e6, 1.0) < 1.0


@given(Q, Q, DQ, B)
def test_cost_is_monotone_in_qy_and_qn(qy, qn, dq, b):
    assert lmsr.cost(qy + dq, qn, b) > lmsr.cost(qy, qn, b) - 1e-9
    assert lmsr.cost(qy, qn + dq, b) > lmsr.cost(qy, qn, b) - 1e-9


@given(st.floats(-50, 50), st.floats(-50, 50), DQ, B)
def test_yes_raises_and_no_lowers_the_price(qy, qn, dq, b):
    p = lmsr.price(qy, qn, b)
    assert lmsr.price(qy + dq, qn, b) >= p
    assert lmsr.price(qy, qn + dq, b) <= p


@given(P, P, B)
def test_closed_form_cost_matches_cost_function(p, p2, b):
    assume(abs(p - p2) > 1e-6)
    qy, qn = lmsr.initial_state(p, b)
    assert close(lmsr.price(qy, qn, b), p)
    before = lmsr.cost(qy, qn, b)
    if p2 > p:
        c, s = lmsr.buy_yes(p, p2, b)
        after = lmsr.cost(qy + s, qn, b)
        assert close(lmsr.price(qy + s, qn, b), p2)
    else:
        c, s = lmsr.buy_no(p, p2, b)
        after = lmsr.cost(qy, qn + s, b)
        assert close(lmsr.price(qy, qn + s, b), p2)
    assert s > 0 and c > 0
    assert close(c, after - before)


@given(P, P, B)
def test_round_trip_nets_zero(p, p2, b):
    """Buying YES p->p2 then NO p2->p leaves a risk-free YES+NO pair that pays exactly what it cost."""
    assume(p2 > p + 1e-6)
    c_yes, s_yes = lmsr.buy_yes(p, p2, b)
    c_no, s_no = lmsr.buy_no(p2, p, b)
    assert close(s_yes, s_no)
    assert close(c_yes + c_no, s_yes)  # payout is s_yes whichever side wins


@given(st.lists(st.tuples(st.sampled_from(["YES", "NO"]), DQ), max_size=30), B)
def test_market_maker_worst_case_loss_is_at_most_b_ln2(trades, b):
    qy0, qn0 = lmsr.initial_state(0.5, b)
    qy, qn = qy0, qn0
    for side, shares in trades:
        if side == "YES":
            qy += shares
        else:
            qn += shares
    collected = lmsr.cost(qy, qn, b) - lmsr.cost(qy0, qn0, b)
    worst_payout = max(qy - qy0, qn - qn0)
    assert worst_payout - collected <= b * math.log(2) + 1e-9


@given(P, BELIEF, SPEND, B)
def test_budget_trade_never_exceeds_spend_or_overshoots_belief(p, belief, spend, b):
    t = lmsr.budget_trade(p, belief, spend, b)
    if t is None:
        return
    assert 0.0 < t.cost <= spend
    assert t.shares > 0
    assert 0.0 < t.price_after < 1.0
    if t.side == "YES":
        assert belief > p and p < t.price_after <= belief
    else:
        assert belief < p and belief <= t.price_after < p


@given(P, SPEND, B)
def test_budget_trade_skips_beliefs_within_eps(p, spend, b):
    assert lmsr.budget_trade(p, p, spend, b) is None
    assert lmsr.budget_trade(p, min(1.0, p + lmsr.EPS / 2), spend, b) is None


@pytest.mark.parametrize("spend", [0.0, -5.0, float("nan"), float("-inf")])
def test_budget_trade_with_no_budget_does_nothing(spend):
    assert lmsr.budget_trade(0.5, 0.9, spend, 20.0) is None


def test_budget_trade_full_belief_stays_inside_open_interval():
    t = lmsr.budget_trade(0.5, 1.0, 1e9, 20.0)
    assert t is not None and t.price_after < 1.0
    t = lmsr.budget_trade(0.5, 0.0, 1e9, 20.0)
    assert t is not None and t.price_after > 0.0


def test_no_single_trader_can_force_auto_merge():
    """R-MKT-3: 0.50 -> 0.90 costs b*ln5 ~= 32.19 > the 30-credit cap."""
    b, cap = 20.0, 30.0
    c, _ = lmsr.buy_yes(0.5, 0.9, b)
    assert close(c, b * math.log(5))
    assert c == pytest.approx(32.1888, abs=1e-3)
    assert c > cap
    t = lmsr.budget_trade(0.5, 0.98, cap, b)
    assert t is not None and t.price_after < 0.9


@given(P, P, B)
def test_trading_math_matches_the_config_safety_check(p, p2, b):
    """ServerConfig checks R-MKT-3 with config.solo_yes_cost; it must agree with what trades really cost."""
    assume(p2 > p + 1e-6)
    assert lmsr.buy_yes(0.5, 0.9, 20.0)[0] == pytest.approx(solo_yes_cost(0.5, 0.9, 20.0))
    assert lmsr.buy_yes(p, p2, b)[0] == pytest.approx(solo_yes_cost(p, p2, b), rel=1e-9, abs=1e-9)


@pytest.mark.parametrize(
    ("fn", "args"),
    [
        (lmsr.price, (0.0, 0.0, 0.0)),
        (lmsr.logit, (1.0,)),
        (lmsr.logit, (0.0,)),
        (lmsr.buy_yes, (0.6, 0.5, 20.0)),
        (lmsr.buy_no, (0.5, 0.6, 20.0)),
        (lmsr.budget_trade, (0.5, 1.5, 10.0, 20.0)),
        (lmsr.budget_trade, (1.0, 0.5, 10.0, 20.0)),
    ],
)
def test_invalid_inputs_raise(fn, args):
    with pytest.raises(ValueError):
        fn(*args)


@given(st.floats(-700, 700), st.floats(-700, 700))
def test_logaddexp_is_stable(x, y):
    assert close(lmsr.logaddexp(x, y), math.log(math.exp(x) + math.exp(y)))
    assert lmsr.logaddexp(1000.0, 1000.0) == pytest.approx(1000.0 + math.log(2))
