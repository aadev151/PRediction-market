"""WP B. Deterministic fake beliefs (PRM_FAKE_LLM=1) from sha256(trader+diff), nudged by signals (§6.4).

Shaped for the gate of DECISIONS #28 (merge at p ≥ 0.90, reject below 0.60, escalate in between, no human
band), so offline markets and the e2e tests reach a decision whatever the trade order:

- **Quiet diff** (no risk signal): every trader in 0.91–0.98 (skeptic 0.91–0.97). A seller never pushes the
  price below its own belief and four capped buyers can afford 0.50 → 0.91 in round 1, so the closing price
  is ≥ 0.905: **auto-merge**. R-MKT-3 still holds: it takes at least two traders' budgets.
- **High-severity signal**: ``security`` 0.06–0.14, everyone else 0.40 lower (≤ 0.58). The price never
  passes the highest belief, so it stays < 0.60: **blocked**.
- **Low signals only**: ``security`` 0.72–0.78, so the price lands in the escalation band.
- ``coverage`` drops 0.10 for changed source files without a test change, but only when a signal also
  fired. A docstring-only change is not a coverage risk.
"""

from __future__ import annotations

import hashlib

from prm.analysis.diffparse import parse_diff
from prm.analysis.diffstats import compute_stats
from prm.analysis.signals import compute_signals
from prm.config import TraderSpec
from prm.types import BELIEF_MAX, BELIEF_MIN, BeliefResult, EscalationContext, PRBundle, TraderContext

FAKE_MODEL = "fake"
QUIET_MIN = 0.92  # quiet beliefs live in [QUIET_MIN, QUIET_MIN + QUIET_SPAN); see the module docstring
QUIET_SPAN = 0.06
HIGH_PENALTY = 0.40  # max quiet belief 0.98 - 0.40 = 0.58 < 0.60


def _unit(trader: str, diff: str) -> float:
    digest = hashlib.sha256(f"{trader}\n{diff}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2**64


def fake_belief(trader: str, bundle: PRBundle) -> BeliefResult:
    files = parse_diff(bundle.diff)
    ctx = bundle.target.context
    signals = compute_signals(files, ctx)
    high = sorted({s.id for s in signals if s.severity == "high"})
    low = sorted({s.id for s in signals if s.severity == "low"})
    u = _unit(trader, bundle.diff)
    p = QUIET_MIN + QUIET_SPAN * u
    if trader == "skeptic":
        p -= 0.01
    if trader == "coverage" and (high or low) and compute_stats(files, ctx).untested_source_files:
        p -= 0.10
    if trader == "security":
        if high:
            p = 0.06 + 0.08 * u
        elif low:
            p -= 0.20
    elif high:
        p -= HIGH_PENALTY
    p = round(min(BELIEF_MAX, max(BELIEF_MIN, p)), 4)
    return BeliefResult(
        trader=trader,
        p_yes=p,
        rationale=f"Deterministic fake belief: {len(high)} high and {len(low)} low risk signal kinds.",
        key_risks=high[:3],
        model=FAKE_MODEL,
        input_tokens=0,
        output_tokens=0,
        latency_ms=0,
        cost_usd=0.0,
        error=None,
    )


class FakeProvider:
    """Belief provider for tests, CI and offline demos. Never touches the network.

    Escalation rounds return the same belief as round 1: the fake has no opinions to revise, which keeps
    every market fully deterministic.
    """

    async def belief(
        self,
        spec: TraderSpec,
        ctx: TraderContext,
        bundle: PRBundle,
        escalation: EscalationContext | None = None,
    ) -> BeliefResult:
        return fake_belief(spec.name, bundle)
