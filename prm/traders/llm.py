"""WP B. Anthropic messages.parse -> BeliefResult; never raises. PLAN §6.4.

The call shape is exactly PLAN §6.4 (verified against the installed SDK, anthropic 2.6.0:
``AsyncMessages.parse(..., output_format=<pydantic type>)`` returns a ``ParsedMessage`` with
``parsed_output``, ``usage`` and ``stop_reason``). No ``temperature``/``top_p``/``top_k``/``thinking``.
The API key is read by the SDK from ``ANTHROPIC_API_KEY`` when the client is first needed.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from typing import Any

from anthropic import AsyncAnthropic
from pydantic import BaseModel, Field

from prm.config import ServerConfig, TraderSpec
from prm.traders.base import abstain
from prm.traders.context import load_prompt
from prm.types import BELIEF_MAX, BELIEF_MIN, BeliefResult, PRBundle, TraderContext

MAX_TOKENS = 4000
TIMEOUT_S = 45.0
MAX_RETRIES = 2
RATIONALE_MAX = 600
RISKS_MAX = 3
RISK_ITEM_MAX = 200
ABSTAIN_STOP_REASONS = frozenset({"refusal", "max_tokens"})


class Belief(BaseModel):
    p_yes: float = Field(
        description="Probability in [0,1] that this PR passes ALL hidden holdout tests and the red team"
    )
    rationale: str = Field(description="At most 3 sentences")
    key_risks: list[str] = Field(description="At most 3 short items")


def make_client() -> AsyncAnthropic:
    return AsyncAnthropic(timeout=TIMEOUT_S, max_retries=MAX_RETRIES)


class LLMProvider:
    """One ``messages.parse`` call per trader per market. Every failure is an abstention."""

    def __init__(
        self,
        cfg: ServerConfig,
        client: Any | None = None,
        clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.cfg = cfg
        self._client = client
        self._clock = clock

    def _cost(self, model: str, input_tokens: int, output_tokens: int) -> float:
        try:
            return self.cfg.cost_usd(model, input_tokens, output_tokens)
        except KeyError:  # the roster is validated against pricing; an ad-hoc spec may not be
            return 0.0

    async def belief(
        self, spec: TraderSpec, ctx: TraderContext, bundle: PRBundle | None = None
    ) -> BeliefResult:
        start = self._clock()
        try:
            if self._client is None:
                self._client = make_client()
            resp = await self._client.messages.parse(
                model=spec.model,
                max_tokens=MAX_TOKENS,
                system=ctx.system_prompt,
                messages=[{"role": "user", "content": ctx.user_prompt}],
                output_format=Belief,
            )
        except Exception as exc:  # a trader never raises
            latency_ms = int((self._clock() - start) * 1000)
            # Type only: an SDK error message can echo request data (DECISIONS #34).
            return abstain(spec.name, spec.model, f"llm error: {type(exc).__name__}", latency_ms=latency_ms)

        latency_ms = int((self._clock() - start) * 1000)
        usage = getattr(resp, "usage", None)
        in_tok = int(getattr(usage, "input_tokens", 0) or 0)
        out_tok = int(getattr(usage, "output_tokens", 0) or 0)
        spent = {
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "latency_ms": latency_ms,
            "cost_usd": self._cost(spec.model, in_tok, out_tok),
        }
        stop = getattr(resp, "stop_reason", None)
        if stop in ABSTAIN_STOP_REASONS:
            return abstain(spec.name, spec.model, f"stop_reason={stop}", **spent)
        try:
            parsed = resp.parsed_output
        except Exception as exc:
            return abstain(spec.name, spec.model, f"unparsable output: {type(exc).__name__}", **spent)
        if not isinstance(parsed, Belief) or not math.isfinite(parsed.p_yes):
            return abstain(spec.name, spec.model, "no valid structured belief in the response", **spent)
        return BeliefResult(
            trader=spec.name,
            p_yes=min(BELIEF_MAX, max(BELIEF_MIN, parsed.p_yes)),
            rationale=parsed.rationale.strip()[:RATIONALE_MAX],
            key_risks=[r.strip()[:RISK_ITEM_MAX] for r in parsed.key_risks[:RISKS_MAX]],
            model=spec.model,
            error=None,
            **spent,
        )


SMOKE_DIFF = """\
diff --git a/calc.py b/calc.py
--- a/calc.py
+++ b/calc.py
@@ -1,2 +1,2 @@
 def add(a, b):
-    return a + b
+    return a - b
"""


def smoke_context(role: str = "diff_reader") -> TraderContext:
    return TraderContext(
        trader=role,
        system_prompt=f"{load_prompt('_common')}\n\n{load_prompt(role)}",
        user_prompt=f"<untrusted_diff>\n{SMOKE_DIFF}</untrusted_diff>\n\nGive your probability.",
    )


async def smoke(cfg: ServerConfig, provider: LLMProvider | None = None) -> list[BeliefResult]:
    """One real call per distinct model in the roster (``make smoke-llm``)."""
    provider = provider or LLMProvider(cfg)
    seen: dict[str, TraderSpec] = {}
    for spec in cfg.traders:
        seen.setdefault(spec.model, spec)
    return [await provider.belief(spec, smoke_context(spec.name)) for spec in seen.values()]
