"""The fake traders through the REAL MarketEngine (default ServerConfig: b=20, 3 rounds, caps 25,
2 escalations).

Offline demos and the e2e test rely on this: with no human band (DECISIONS #28) a benign diff must auto-merge
and a removed-check diff must be blocked, for every trade order. A diff in the middle band must go through the
engine's escalation rounds on WP A's contract (#39), with B receiving an ``EscalationContext``, and still end
in success or failure, never pending.
"""

import random

import pytest

from prm.config import ServerConfig, TargetGateOverrides
from prm.db import make_session_factory
from prm.market.engine import MarketEngine
from prm.models import Market
from prm.traders.fake import FakeProvider, fake_belief
from prm.traders.roster import EngineProvider
from prm.types import EscalationContext
from tests.traders._bundles import BUG_DIFF, GOOD_DIFF, make_bundle

SEEDS = range(20)

DOCSTRING_TWEAK = """\
diff --git a/svc/handlers.py b/svc/handlers.py
--- a/svc/handlers.py
+++ b/svc/handlers.py
@@ -10,2 +10,3 @@ def list_items(limit):
 def list_items(limit):
+    \"\"\"Return up to `limit` items, oldest first.\"\"\"
     return items[:limit]
"""

COMMENT_ONLY = """\
diff --git a/lib/util.js b/lib/util.js
--- a/lib/util.js
+++ b/lib/util.js
@@ -1,2 +1,3 @@
+// Helpers shared by the request handlers.
 function clamp(x, lo, hi) {
   return Math.min(hi, Math.max(lo, x));
"""

DROP_AUTH_DEPENDENCY = """\
diff --git a/svc/app.py b/svc/app.py
--- a/svc/app.py
+++ b/svc/app.py
@@ -84,7 +84,6 @@ def create_app(token):
     @app.delete("/items/{item_id}", status_code=204)
     def delete_item(
         item_id: int,
-        _admin: Annotated[None, Depends(require_admin)],
     ) -> Response:
         if item_id not in items:
             raise HTTPException(status_code=404)
"""


async def close_and_gate(tmp_path, diff: str, seed: int) -> tuple[str, float]:
    bundle = make_bundle(tmp_path, diff=diff)
    engine = MarketEngine(
        make_session_factory("sqlite://"),
        ServerConfig(),
        EngineProvider(FakeProvider()),
        rng=random.Random(seed),  # noqa: S311 - trade order, not security
    )
    mid = engine.create(engine.ensure_repo(bundle.repo), bundle)
    beliefs = await engine.run_agents(mid, bundle)
    assert all(not b.abstained for b in beliefs)
    price = engine.close(mid)
    return engine.gate(mid, TargetGateOverrides()).state, price


@pytest.mark.parametrize("diff", [GOOD_DIFF, DOCSTRING_TWEAK, COMMENT_ONLY], ids=["docstring", "tweak", "js"])
async def test_benign_diff_auto_merges_for_every_trade_order(tmp_path, diff):
    for seed in SEEDS:
        state, price = await close_and_gate(tmp_path, diff, seed)
        assert (state, price >= 0.90) == ("success", True), (seed, price)


@pytest.mark.parametrize(
    "diff", [BUG_DIFF, DROP_AUTH_DEPENDENCY], ids=["removed-guard", "dropped-dependency"]
)
async def test_removed_check_is_blocked_for_every_trade_order(tmp_path, diff):
    for seed in SEEDS:
        state, price = await close_and_gate(tmp_path, diff, seed)
        assert (state, price < 0.60) == ("failure", True), (seed, price)


# A docstring in a sensitive path: only a low-severity signal, so security lands near 0.75 and coverage near
# 0.84 while the others stay above 0.90: the price sits in [0.60, 0.90) and the engine escalates.
SENSITIVE_DOCSTRING = """\
diff --git a/app/auth.py b/app/auth.py
--- a/app/auth.py
+++ b/app/auth.py
@@ -1,2 +1,3 @@
 def bearer_token(header):
+    \"\"\"Return the bearer token or None.\"\"\"
     return parse(header)
"""


class _Spy(EngineProvider):
    """The real adapter, recording every EscalationContext the engine hands it."""

    def __init__(self, provider):
        super().__init__(provider)
        self.seen: list[tuple[str, EscalationContext]] = []

    async def get_belief(self, spec, bundle, escalation=None):
        if escalation is not None:
            self.seen.append((spec.name, escalation))
        return await super().get_belief(spec, bundle, escalation=escalation)


async def test_middle_band_goes_through_escalation_on_the_engine_contract(tmp_path):
    """Whether phase 0 ends in the band depends on trade order (a bull trading last can lift it past 0.90),
    so the contract is checked on every seed and escalation must actually happen on most of them."""
    bundle = make_bundle(tmp_path, diff=SENSITIVE_DOCSTRING)
    cfg = ServerConfig()
    round_one = {s.name: fake_belief(s.name, bundle) for s in cfg.traders}
    assert 0.60 <= min(b.p_yes for b in round_one.values()) < 0.90  # the scenario really is in the band
    escalated = 0
    for seed in SEEDS:
        spy = _Spy(FakeProvider())
        engine = MarketEngine(make_session_factory("sqlite://"), cfg, spy, rng=random.Random(seed))  # noqa: S311
        mid = engine.create(engine.ensure_repo(bundle.repo), bundle)
        beliefs = await engine.run_agents(mid, bundle)
        assert not [b.error for b in beliefs if b.error], seed  # no TypeError abstentions: the seam matches
        with engine._sf() as s:
            escalations = s.get(Market, mid).escalations
        escalated += escalations >= 1
        assert len(spy.seen) == escalations * len(cfg.traders), seed  # every trader re-asked, every round
        for name, ctx in spy.seen:
            assert isinstance(ctx, EscalationContext) and 1 <= ctx.round <= ctx.max_rounds
            assert ctx.previous_p_yes == round_one[name].p_yes  # the fake doesn't revise its view
            assert name not in {p.trader for p in ctx.peers} and len(ctx.peers) == len(cfg.traders) - 1
        engine.close(mid)
        decision = engine.gate(mid, TargetGateOverrides())
        assert decision.state in ("success", "failure"), (seed, decision)  # never pending (#28)
    assert escalated >= len(SEEDS) // 2, escalated  # 19 of 20 trade orders escalate today
