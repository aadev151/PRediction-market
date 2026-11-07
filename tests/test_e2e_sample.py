"""End to end on the sample target (PLAN §7): ``run_local_market`` with the fake LLM through the real engine,
settled by the REAL post-merge oracle (DECISIONS #57) in a throwaway git repo.

bug_auth: merged, then reverted -> NO. good_docstring: merged, window fast-forwarded -> YES. Demo markets
always pay out (#41). A spy around ``settle_postmerge`` checks that the patch really is in the merge commit
and records the oracle's reason.
"""

import random
import subprocess
from pathlib import Path

import pytest
from sqlalchemy import select

from prm import pipeline
from prm.config import RuntimeEnv, ServerConfig
from prm.db import make_session_factory
from prm.market.engine import MarketEngine
from prm.models import Market, Position
from prm.pipeline import PipelineDeps, run_local_market
from prm.settle.postmerge import evaluate, settle_postmerge
from prm.traders.fake import FakeProvider
from prm.traders.roster import EngineProvider

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "examples" / "sample-target"
PATCHES = ROOT / "examples" / "patches"
APP = "notes_app/app.py"
ADMIN_DEP = "Depends(require_admin)"
DOCSTRING = '"""Return one note by id'


@pytest.fixture
def oracle(monkeypatch):
    """Wraps the real oracle: records (merged app.py, verdict reason) for each settlement."""
    seen: list[tuple[str, str]] = []

    def spy(repo, pr, *, now, branch="HEAD", **kw):
        merged = subprocess.run(
            ["git", "-C", str(repo), "show", f"{pr.merge_sha}:{APP}"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        seen.append((merged, evaluate(repo, pr, now=now, branch=branch).reason))
        return settle_postmerge(repo, pr, now=now, branch=branch, **kw)

    monkeypatch.setattr(pipeline, "settle_postmerge", spy)
    return seen


def deps_for(tmp_path, seed):
    cfg = ServerConfig()
    sf = make_session_factory("sqlite://")
    engine = MarketEngine(sf, cfg, EngineProvider(FakeProvider()), rng=random.Random(seed))  # noqa: S311
    return PipelineDeps(RuntimeEnv(cache_dir=tmp_path / "cache", fake_llm=True), cfg, sf, engine, None)


@pytest.mark.parametrize("seed", [0, 1, 2])
async def test_bug_auth_is_blocked_reverted_and_settles_no(tmp_path, seed, oracle):
    deps = deps_for(tmp_path, seed)
    statuses = []
    mid = await run_local_market(
        TARGET, PATCHES / "bug_auth.patch", repo="local/sample-target", after_merge="revert",
        deps=deps, statuses=statuses,
    )  # fmt: skip
    with deps.session_factory() as s:
        m = s.get(Market, mid)
        positions = list(s.scalars(select(Position).where(Position.market_id == mid)))
    assert m.final_price < 0.60
    assert (m.gate_state, m.state, m.outcome, m.resolution, m.source) == (
        "failure",
        "SETTLED",
        "NO",
        "paid",
        "demo",
    )
    merged, reason = oracle[0]
    assert reason == "reverted"
    assert merged.count(ADMIN_DEP) == (TARGET / APP).read_text().count(ADMIN_DEP) - 1
    assert positions and all(p.payout is not None for p in positions)
    assert sum(p.payout for p in positions) == pytest.approx(sum(p.no_shares for p in positions))
    assert [s for s, _, _ in statuses][:2] == ["pending", "failure"]
    assert statuses[-1][1].startswith("blocked (p=0.")
    assert statuses[-1][1].endswith("· settled NO · market right")


@pytest.mark.parametrize("seed", [0, 1, 2])
async def test_good_docstring_auto_merges_and_settles_yes(tmp_path, seed, oracle):
    deps = deps_for(tmp_path, seed)
    mid = await run_local_market(
        TARGET, PATCHES / "good_docstring.patch", repo="local/sample-target", after_merge="quiet", deps=deps
    )
    with deps.session_factory() as s:
        m = s.get(Market, mid)
    assert m.final_price >= 0.90
    assert (m.gate_state, m.state, m.outcome, m.resolution) == ("success", "SETTLED", "YES", "paid")
    merged, reason = oracle[0]
    assert reason == "window_elapsed" and DOCSTRING in merged
    status = deps.engine.status_description(mid)
    assert status.startswith("auto-merge OK (p=0.9") and status.endswith("· settled YES · market right")


async def test_every_demo_run_opens_a_fresh_market(tmp_path):
    deps = deps_for(tmp_path, 0)
    patch = PATCHES / "good_docstring.patch"
    a = await run_local_market(TARGET, patch, repo="local/sample-target", deps=deps)
    b = await run_local_market(TARGET, patch, repo="local/sample-target", deps=deps)
    assert a != b
    assert not list((tmp_path / "cache").iterdir())  # temp copies and the demo repo cleaned up
