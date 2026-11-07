"""WP A. ALLOWED transitions, ACTIONS, transition(), require_action(). PLAN §6.7.

"Agents are suggestions, states are laws." Only the engine calls ``transition``; every call is checked against
``ALLOWED`` and leaves an append-only ``events`` row. Terminal states (SETTLED, VOID) have no exits.
"""

from __future__ import annotations

from datetime import datetime

from sqlalchemy.orm import Session

from prm.models import Event, Market, utcnow
from prm.types import State

S = State
ALLOWED: dict[State, frozenset[State]] = {
    S.CREATED: frozenset({S.CONTEXT_READY, S.VOID}),
    S.CONTEXT_READY: frozenset({S.TRADING, S.VOID}),
    S.TRADING: frozenset({S.HUMAN_WINDOW, S.CLOSED, S.VOID}),
    S.HUMAN_WINDOW: frozenset({S.CLOSED, S.VOID}),
    S.CLOSED: frozenset({S.GATED, S.VOID}),
    S.GATED: frozenset({S.SETTLING, S.VOID}),
    S.SETTLING: frozenset({S.SETTLED, S.VOID}),
    S.SETTLED: frozenset(),
    S.VOID: frozenset(),
}

ACTIONS: dict[str, frozenset[State]] = {
    "agent_trade": frozenset({S.TRADING}),
    "human_trade": frozenset({S.HUMAN_WINDOW}),
    "gate": frozenset({S.CLOSED}),
    "settle": frozenset({S.GATED}),
    "payout": frozenset({S.SETTLING}),
}


class IllegalTransition(Exception):
    """A state change not in ``ALLOWED``."""


class IllegalAction(Exception):
    """An action attempted in a state that does not permit it."""


def check_transition(from_state: State, to_state: State) -> None:
    if to_state not in ALLOWED[from_state]:
        raise IllegalTransition(f"{from_state} -> {to_state} is not allowed")


def transition(
    session: Session,
    market: Market,
    to_state: State,
    actor: str,
    detail: str = "",
    *,
    at: datetime | None = None,
) -> None:
    """Move ``market`` to ``to_state`` and append an audit event. Raises ``IllegalTransition``."""
    from_state = State(market.state)
    check_transition(from_state, State(to_state))
    if market.id is None:
        session.flush()
    session.add(
        Event(
            market_id=market.id,
            from_state=from_state.value,
            to_state=State(to_state).value,
            actor=actor,
            detail=detail,
            created_at=at or utcnow(),
        )
    )
    market.state = State(to_state).value


def require_action(market: Market, action: str) -> None:
    """Raise ``IllegalAction`` unless ``action`` is permitted in the market's current state."""
    allowed = ACTIONS.get(action)
    if allowed is None:
        raise IllegalAction(f"unknown action {action!r}")
    if State(market.state) not in allowed:
        raise IllegalAction(f"{action} is not allowed in state {market.state}")
