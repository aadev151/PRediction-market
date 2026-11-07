from pathlib import Path

import pytest

from prm.config import parse_target_config
from prm.types import (
    REVEAL_STATES,
    TERMINAL_STATES,
    BeliefResult,
    Finding,
    GateDecision,
    PRBundle,
    SettlementResult,
    State,
    TradeResult,
)


def belief(**kw):
    base = dict(
        trader="diff_reader",
        p_yes=0.7,
        rationale="ok",
        key_risks=[],
        model="m",
        input_tokens=10,
        output_tokens=5,
        latency_ms=100,
        cost_usd=0.0001,
        error=None,
    )
    return BeliefResult(**{**base, **kw})


def test_states():
    assert [s.value for s in State] == [
        "CREATED",
        "CONTEXT_READY",
        "TRADING",
        "HUMAN_WINDOW",
        "CLOSED",
        "GATED",
        "SETTLING",
        "SETTLED",
        "VOID",
    ]
    assert State("VOID") is State.VOID and State.VOID == "VOID"
    assert TERMINAL_STATES == {State.SETTLED, State.VOID}
    assert TERMINAL_STATES <= REVEAL_STATES
    assert State.TRADING not in REVEAL_STATES and State.HUMAN_WINDOW not in REVEAL_STATES


def test_belief_valid_and_abstaining():
    assert not belief().abstained
    b = belief(p_yes=None, error="refusal")
    assert b.abstained


@pytest.mark.parametrize(
    "kw",
    [
        {"p_yes": None},  # abstaining without an error
        {"p_yes": 1.2},
        {"p_yes": float("nan")},
        {"input_tokens": -1},
        {"cost_usd": -0.1},
    ],
)
def test_belief_rejects(kw):
    with pytest.raises(ValueError):
        belief(**kw)


def test_trade_result_validation():
    TradeResult("security", "NO", 10.0, 5.0, 0.5, 0.3, 1)
    with pytest.raises(ValueError):
        TradeResult("security", "NO", 10.0, 5.0, 0.5, 1.0, 1)
    with pytest.raises(ValueError):
        TradeResult("security", "NO", -1.0, 5.0, 0.5, 0.3, 1)


def test_gate_without_price_must_fail_closed():
    GateDecision("failure", "market void: human review required", None)
    GateDecision("success", "auto-merge OK (p=0.94)", 0.94)
    with pytest.raises(ValueError, match="fail closed"):
        GateDecision("success", "?", None)
    with pytest.raises(ValueError):
        GateDecision("pending", "?", 1.5)


def test_settlement_result_invariants():
    ok = SettlementResult("YES", 12, 12, [Finding("f1", "t", "low", False)], 1500, None)
    assert ok.outcome == "YES"
    err = SettlementResult.infra_error("holdouts missing")
    assert err.outcome is None and err.error == "holdouts missing"
    with pytest.raises(ValueError):
        SettlementResult(None, 0, 0, [], 0, None)  # no outcome and no error
    with pytest.raises(ValueError):
        SettlementResult("NO", 0, 0, [], 0, "boom")  # outcome with an error
    with pytest.raises(ValueError):
        SettlementResult("NO", 5, 3, [], 0, None)  # passed > total


def test_pr_bundle_holds_target_config(tmp_path: Path):
    cfg = parse_target_config('[target]\nname="t"\nholdouts_repo="o/r"\n[settle]\ncommand="pytest"\n')
    bundle = PRBundle("demo", "o/t", None, None, None, "diff", "desc", tmp_path, tmp_path, cfg)
    assert bundle.target.target.name == "t"
    with pytest.raises(AttributeError):
        bundle.diff = "x"  # type: ignore[misc]
