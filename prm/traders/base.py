"""WP B. The belief-provider protocol shared by the llm, fake and replay traders."""

from __future__ import annotations

from typing import Protocol

from prm.config import TraderSpec
from prm.types import BeliefResult, PRBundle, TraderContext

ERROR_MAX = 300


class BeliefProvider(Protocol):
    """Turns one trader's context into a belief. Implementations never raise; they abstain."""

    async def belief(self, spec: TraderSpec, ctx: TraderContext, bundle: PRBundle) -> BeliefResult: ...


def abstain(trader: str, model: str, error: str, **usage: int | float) -> BeliefResult:
    """An abstaining belief (``p_yes=None``). ``usage`` may carry tokens/latency/cost already spent."""
    return BeliefResult(
        trader=trader,
        p_yes=None,
        rationale="",
        key_risks=[],
        model=model,
        input_tokens=int(usage.get("input_tokens", 0)),
        output_tokens=int(usage.get("output_tokens", 0)),
        latency_ms=int(usage.get("latency_ms", 0)),
        cost_usd=float(usage.get("cost_usd", 0.0)),
        error=(error or "unknown error")[:ERROR_MAX],
    )
