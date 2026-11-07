"""State machine laws (PLAN §6.7, R-SM-1)."""

import itertools

import pytest
from hypothesis import given
from hypothesis import strategies as st

from prm.db import make_session_factory
from prm.market.state import (
    ACTIONS,
    ALLOWED,
    IllegalAction,
    IllegalTransition,
    check_transition,
    require_action,
    transition,
)
from prm.models import Event, Market, Repo
from prm.types import TERMINAL_STATES, State

LEGAL = {(a, b) for a, targets in ALLOWED.items() for b in targets}
ILLEGAL = [(a, b) for a, b in itertools.product(State, State) if (a, b) not in LEGAL]

SF = make_session_factory("sqlite://")


def _new_market(s, state: State = State.CREATED) -> Market:
    repo = s.query(Repo).first() or Repo(full_name="acme/widgets")
    s.add(repo)
    s.flush()
    m = Market(
        repo_id=repo.id, source="league", pr_number=None, head_sha=None, state=state.value,
        b=20.0, q_yes=0.0, q_no=0.0, initial_price=0.5,
    )  # fmt: skip
    s.add(m)
    return m


def test_allowed_matches_plan_exactly():
    s = State
    assert ALLOWED == {
        s.CREATED: {s.CONTEXT_READY, s.VOID},
        s.CONTEXT_READY: {s.TRADING, s.VOID},
        s.TRADING: {s.HUMAN_WINDOW, s.CLOSED, s.VOID},
        s.HUMAN_WINDOW: {s.CLOSED, s.VOID},
        s.CLOSED: {s.GATED, s.VOID},
        s.GATED: {s.SETTLING, s.VOID},
        s.SETTLING: {s.SETTLED, s.VOID},
        s.SETTLED: set(),
        s.VOID: set(),
    }
    assert set(ALLOWED) == set(State)


def test_terminal_states_have_no_exits():
    for t in TERMINAL_STATES:
        assert ALLOWED[t] == frozenset()
    assert {s for s, targets in ALLOWED.items() if not targets} == TERMINAL_STATES


def test_every_non_terminal_state_can_void():
    for s in set(State) - TERMINAL_STATES:
        assert State.VOID in ALLOWED[s]


@pytest.mark.parametrize(("a", "b"), ILLEGAL, ids=[f"{a}->{b}" for a, b in ILLEGAL])
def test_every_illegal_pair_raises(a, b):
    with pytest.raises(IllegalTransition):
        check_transition(a, b)
    with SF() as s:
        m = _new_market(s, a)
        s.flush()
        with pytest.raises(IllegalTransition):
            transition(s, m, b, actor="test")
        assert m.state == a.value
        assert s.query(Event).filter_by(market_id=m.id).count() == 0
        s.rollback()


def test_legal_transition_appends_an_event_row():
    with SF() as s:
        m = _new_market(s)
        transition(s, m, State.CONTEXT_READY, actor="engine", detail="context built")
        s.flush()
        assert m.state == State.CONTEXT_READY.value
        ev = s.query(Event).filter_by(market_id=m.id).one()
        assert (ev.from_state, ev.to_state, ev.actor, ev.detail) == (
            "CREATED",
            "CONTEXT_READY",
            "engine",
            "context built",
        )
        s.rollback()


@given(st.lists(st.integers(min_value=0, max_value=100), min_size=20, max_size=20))
def test_random_walks_over_legal_transitions_end_in_settled_or_void(choices):
    with SF() as s:
        m = _new_market(s)
        path = [State.CREATED]
        for c in choices:
            options = sorted(ALLOWED[State(m.state)])
            if not options:
                break
            transition(s, m, options[c % len(options)], actor="walk")
            path.append(State(m.state))
        # Longest legal path is 7 steps, so 20 choices always reach a terminal state.
        assert State(m.state) in TERMINAL_STATES
        s.flush()
        assert s.query(Event).filter_by(market_id=m.id).count() == len(path) - 1
        s.rollback()


def test_actions_match_plan():
    assert ACTIONS == {
        "agent_trade": {State.TRADING},
        "human_trade": {State.HUMAN_WINDOW},
        "gate": {State.CLOSED},
        "settle": {State.GATED},
        "payout": {State.SETTLING},
    }


@pytest.mark.parametrize("action", sorted(ACTIONS))
def test_require_action_only_in_its_states(action):
    for st_ in State:
        m = Market(state=st_.value)
        if st_ in ACTIONS[action]:
            require_action(m, action)
        else:
            with pytest.raises(IllegalAction):
                require_action(m, action)


def test_second_payout_is_refused_by_the_state_machine():
    """Once SETTLED, 'payout' is not an allowed action, so the engine can never pay twice."""
    m = Market(state=State.SETTLING.value)
    require_action(m, "payout")
    m.state = State.SETTLED.value
    with pytest.raises(IllegalAction):
        require_action(m, "payout")


def test_unknown_action_raises():
    with pytest.raises(IllegalAction):
        require_action(Market(state=State.TRADING.value), "merge_now")
