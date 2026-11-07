"""WP B. Deterministic fake beliefs (PRM_FAKE_LLM=1) from sha256(trader+diff), nudged by signals (§6.4).

Shape (so the e2e tests mean something): a quiet diff gets beliefs in 0.55–0.90 (skeptic 0.10 lower). Any
high-severity signal sends ``security`` to ~0.1 and every other trader down by 0.35, so all beliefs are
≤ 0.55 and the LMSR price (which never passes the highest belief) can't reach the 0.60 human-review band.
"""

from __future__ import annotations

import hashlib

from prm.analysis.diffparse import parse_diff
from prm.analysis.diffstats import compute_stats
from prm.analysis.signals import compute_signals
from prm.config import TraderSpec
from prm.types import BELIEF_MAX, BELIEF_MIN, BeliefResult, EscalationContext, PRBundle, TraderContext

FAKE_MODEL = "fake"


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
    p = 0.55 + 0.35 * u
    if trader == "skeptic":
        p -= 0.10
    if trader == "coverage" and compute_stats(files, ctx).untested_source_files:
        p -= 0.10
    if trader == "security":
        if high:
            p = 0.06 + 0.08 * u
        elif low:
            p -= 0.15
    elif high:
        p -= 0.35
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
