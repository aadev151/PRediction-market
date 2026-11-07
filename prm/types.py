"""Shared types: the contract between work packages (PLAN §6.2).

Every package imports from here. Changing a type is a cross-package change: add a line to docs/DECISIONS.md.
The light validation in ``__post_init__`` encodes fail-closed rules so a bad value can't travel silently.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from prm.config import TargetConfig

Side = Literal["YES", "NO"]
Outcome = Literal["YES", "NO"]
Source = Literal["github", "league", "demo"]
GateState = Literal["success", "pending", "failure"]
Severity = Literal["high", "low"]
TraderKind = Literal["agent", "human"]
TraderRole = Literal["diff_reader", "coverage", "security", "skeptic"]

TRADER_ROLES: tuple[TraderRole, ...] = ("diff_reader", "coverage", "security", "skeptic")

# Client-side clamp for every agent belief (R-TRD-1).
BELIEF_MIN = 0.02
BELIEF_MAX = 0.98


class State(StrEnum):
    CREATED = "CREATED"
    CONTEXT_READY = "CONTEXT_READY"
    TRADING = "TRADING"
    HUMAN_WINDOW = "HUMAN_WINDOW"
    CLOSED = "CLOSED"
    GATED = "GATED"
    SETTLING = "SETTLING"
    SETTLED = "SETTLED"
    VOID = "VOID"


TERMINAL_STATES: frozenset[State] = frozenset({State.SETTLED, State.VOID})
# Beliefs and rationales are only exposed by the API once the market is in one of these (PLAN §4).
REVEAL_STATES: frozenset[State] = frozenset(
    {State.CLOSED, State.GATED, State.SETTLING, State.SETTLED, State.VOID}
)


def _check_prob(name: str, value: float, *, open_interval: bool = False) -> None:
    if not math.isfinite(value):
        raise ValueError(f"{name} must be finite, got {value!r}")
    if open_interval and not 0.0 < value < 1.0:
        raise ValueError(f"{name} must be in (0, 1), got {value!r}")
    if not 0.0 <= value <= 1.0:
        raise ValueError(f"{name} must be in [0, 1], got {value!r}")


@dataclass(frozen=True)
class PRBundle:
    """Everything the factory knows about one PR. ``target`` is parsed from the BASE tree's .prm.toml."""

    source: Source
    repo: str
    pr_number: int | None
    head_sha: str | None
    base_sha: str | None
    diff: str
    description: str
    base_dir: Path
    head_dir: Path
    target: TargetConfig


@dataclass(frozen=True)
class TraderContext:
    """The exact prompts one trader sees. Built by traders/context.py; nothing else reaches the LLM."""

    trader: str
    system_prompt: str
    user_prompt: str


@dataclass(frozen=True)
class BeliefResult:
    """One trader's belief. ``p_yes=None`` means it abstained, which always carries an ``error``."""

    trader: str
    p_yes: float | None
    rationale: str
    key_risks: list[str]
    model: str
    input_tokens: int
    output_tokens: int
    latency_ms: int
    cost_usd: float
    error: str | None

    def __post_init__(self) -> None:
        if self.p_yes is None:
            if not self.error:
                raise ValueError("an abstaining belief (p_yes=None) must carry an error")
        else:
            _check_prob("p_yes", self.p_yes)
        if min(self.input_tokens, self.output_tokens, self.latency_ms) < 0 or self.cost_usd < 0:
            raise ValueError("tokens, latency and cost must be non-negative")

    @property
    def abstained(self) -> bool:
        return self.p_yes is None


@dataclass(frozen=True)
class PeerView:
    """What one trader shows the others in an escalation round: its reasoning, never its number.

    The rationale and risks are LLM output written while reading an untrusted diff, so a provider must render
    them as untrusted data (inside ``<untrusted_*>`` tags), exactly like the diff (R-TRD-3).
    """

    trader: str
    rationale: str
    key_risks: list[str]


@dataclass(frozen=True)
class EscalationContext:
    """Input to an escalation re-estimate (R-TRD-2, DECISIONS #28): the live price, the trader's own previous
    belief (None if it abstained), and every other responding trader's rationale."""

    round: int
    max_rounds: int
    price: float
    previous_p_yes: float | None
    previous_rationale: str
    peers: tuple[PeerView, ...]

    def __post_init__(self) -> None:
        if not 1 <= self.round <= self.max_rounds:
            raise ValueError("need 1 <= round <= max_rounds")
        _check_prob("price", self.price, open_interval=True)
        if self.previous_p_yes is not None:
            _check_prob("previous_p_yes", self.previous_p_yes)


@dataclass(frozen=True)
class TradeResult:
    trader: str
    side: Side
    shares: float
    cost: float
    price_before: float
    price_after: float
    round: int

    def __post_init__(self) -> None:
        _check_prob("price_before", self.price_before, open_interval=True)
        _check_prob("price_after", self.price_after, open_interval=True)
        if self.shares < 0 or self.cost < 0:
            raise ValueError("shares and cost must be non-negative")
        if self.round < 0:
            raise ValueError("round must be >= 0")


@dataclass(frozen=True)
class GateDecision:
    """What the `pr-market` status says. No price (e.g. a VOID market) can only ever mean failure."""

    state: GateState
    description: str
    price: float | None

    def __post_init__(self) -> None:
        if self.price is None:
            if self.state != "failure":
                raise ValueError("a gate decision without a price must be 'failure' (fail closed)")
        else:
            _check_prob("price", self.price)


@dataclass(frozen=True)
class Finding:
    """A red-team finding. Only ids, titles and severities leave the sandbox."""

    id: str
    title: str
    severity: Severity
    confirmed: bool


@dataclass(frozen=True)
class SettlementResult:
    """The oracle's answer. Counts only, never holdout names or content (R-SET-5).

    ``outcome`` is None exactly when ``error`` is set, which means an infra failure and VOIDs the market.
    A candidate crash or timeout is not an error: it is outcome NO.
    """

    outcome: Outcome | None
    holdouts_passed: int
    holdouts_total: int
    findings: list[Finding]
    duration_ms: int
    error: str | None

    def __post_init__(self) -> None:
        if (self.outcome is None) != bool(self.error):
            raise ValueError("outcome must be None exactly when error is set")
        if not 0 <= self.holdouts_passed <= self.holdouts_total:
            raise ValueError("need 0 <= holdouts_passed <= holdouts_total")
        if self.duration_ms < 0:
            raise ValueError("duration_ms must be non-negative")

    @classmethod
    def infra_error(cls, error: str, duration_ms: int = 0) -> SettlementResult:
        return cls(None, 0, 0, [], duration_ms, error)


@dataclass(frozen=True)
class Signal:
    """A language-agnostic risk signal over a diff (PLAN §6.6). ``line`` is the new-file line when known."""

    id: str
    severity: Severity
    file: str
    line: int | None
    excerpt: str
