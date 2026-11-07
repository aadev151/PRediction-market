"""The fake traders through the REAL MarketEngine (default ServerConfig: b=20, 3 rounds, caps 25).

Offline demos and the e2e test rely on this: with no human band (DECISIONS #28) a benign diff must auto-merge
and a removed-check diff must be blocked, for every trade order. Only success/failure are asserted (never the
middle band), so this keeps passing once WP A replaces the pending band with escalation rounds.
"""

import random

import pytest

from prm.config import ServerConfig, TargetGateOverrides
from prm.db import make_session_factory
from prm.market.engine import MarketEngine
from prm.traders.fake import FakeProvider
from prm.traders.roster import EngineProvider
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
