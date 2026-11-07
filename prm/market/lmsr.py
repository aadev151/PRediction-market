"""WP A. Pure closed-form LMSR functions + budget_trade. PLAN §6.3 (hypothesis tests mandatory).

Binary LMSR with liquidity ``b``. ``qy``/``qn`` are the outstanding YES/NO shares; one winning share pays 1.

    price(qy, qn, b) = 1 / (1 + exp(-(qy - qn) / b))
    cost(qy, qn, b)  = b * logaddexp(qy / b, qn / b)

Trades are expressed by their target price, so every cost and share count is closed form. Prices returned here
are clamped into ``[P_MIN, 1 - P_MIN]``: a market can never sit at exactly 0 or 1.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from prm.types import Side

EPS = 0.005  # no trade when the belief is within EPS of the price
P_MIN = 1e-9  # prices never reach exactly 0 or 1
_MIN_SHARES = 1e-12  # below this a trade is numerical noise


@dataclass(frozen=True)
class LMSRTrade:
    side: Side
    price_after: float
    shares: float
    cost: float


def _clamp(p: float) -> float:
    return min(max(p, P_MIN), 1.0 - P_MIN)


def _check_b(b: float) -> None:
    if not (math.isfinite(b) and b > 0):
        raise ValueError(f"b must be finite and > 0, got {b!r}")


def _check_open_prob(name: str, p: float) -> None:
    if not (math.isfinite(p) and 0.0 < p < 1.0):
        raise ValueError(f"{name} must be in (0, 1), got {p!r}")


def logaddexp(x: float, y: float) -> float:
    """Stable ``log(exp(x) + exp(y))``."""
    hi, lo = (x, y) if x >= y else (y, x)
    return hi + math.log1p(math.exp(lo - hi))


def price(qy: float, qn: float, b: float) -> float:
    """YES price for the share state ``(qy, qn)``, clamped into ``(0, 1)``."""
    _check_b(b)
    x = (qy - qn) / b
    if x >= 0:
        p = 1.0 / (1.0 + math.exp(-x))
    else:
        e = math.exp(x)
        p = e / (1.0 + e)
    return _clamp(p)


def cost(qy: float, qn: float, b: float) -> float:
    """LMSR cost function. A trade costs ``cost(after) - cost(before)``."""
    _check_b(b)
    return b * logaddexp(qy / b, qn / b)


def logit(p: float) -> float:
    _check_open_prob("p", p)
    return math.log(p) - math.log1p(-p)


def initial_state(p0: float, b: float) -> tuple[float, float]:
    """Share state ``(qy, qn)`` whose price is ``p0``."""
    _check_b(b)
    return b * logit(p0), 0.0


def buy_yes(p: float, p2: float, b: float) -> tuple[float, float]:
    """``(cost, shares)`` of buying YES to move the price from ``p`` up to ``p2``."""
    _check_b(b)
    _check_open_prob("p", p)
    _check_open_prob("p2", p2)
    if p2 < p:
        raise ValueError(f"buying YES cannot lower the price ({p} -> {p2})")
    return b * (math.log1p(-p) - math.log1p(-p2)), b * (logit(p2) - logit(p))


def buy_no(p: float, p2: float, b: float) -> tuple[float, float]:
    """``(cost, shares)`` of buying NO to move the price from ``p`` down to ``p2``."""
    _check_b(b)
    _check_open_prob("p", p)
    _check_open_prob("p2", p2)
    if p2 > p:
        raise ValueError(f"buying NO cannot raise the price ({p} -> {p2})")
    return b * (math.log(p) - math.log(p2)), b * (logit(p) - logit(p2))


def budget_trade(p: float, belief: float, spend: float, b: float) -> LMSRTrade | None:
    """Move the price toward ``belief`` spending at most ``spend``. None when no trade is warranted.

    Never overshoots the belief and never costs more than ``spend`` (the cost is capped at ``spend`` to absorb
    float rounding of ~1e-15).
    """
    _check_b(b)
    _check_open_prob("p", p)
    if not (math.isfinite(belief) and 0.0 <= belief <= 1.0):
        raise ValueError(f"belief must be in [0, 1], got {belief!r}")
    if not math.isfinite(spend) or spend <= 0:
        return None
    if belief > p + EPS:
        p2 = _clamp(min(belief, 1.0 - (1.0 - p) * math.exp(-spend / b)))
        if p2 <= p:
            return None
        c, s = buy_yes(p, p2, b)
        side: Side = "YES"
    elif belief < p - EPS:
        p2 = _clamp(max(belief, p * math.exp(-spend / b)))
        if p2 >= p:
            return None
        c, s = buy_no(p, p2, b)
        side = "NO"
    else:
        return None
    if s < _MIN_SHARES:
        return None
    return LMSRTrade(side=side, price_after=p2, shares=s, cost=min(c, spend))
