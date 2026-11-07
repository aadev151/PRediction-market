"""End to end on the sample target (PLAN §7): ``run_local_market`` with the fake LLM through the real engine.

The settler is an injected stub (WP D's ``settle_tree`` isn't merged yet). It checks that the patch really
landed in the head tree and returns the outcome the fixture holdouts would give. A variant with the real
settler comes once WP D merges.
"""

import random
from pathlib import Path

import pytest
from sqlalchemy import select

from prm.config import RuntimeEnv, ServerConfig
from prm.db import make_session_factory
from prm.market.engine import MarketEngine
from prm.models import Market, Position
from prm.pipeline import PipelineDeps, run_local_market
from prm.traders.fake import FakeProvider
from prm.traders.roster import EngineProvider
from prm.types import SettlementResult

ROOT = Path(__file__).resolve().parents[1]
TARGET = ROOT / "examples" / "sample-target"
PATCHES = ROOT / "examples" / "patches"
APP = Path("notes_app") / "app.py"
ADMIN_DEP = "Depends(require_admin)"
DOCSTRING = '"""Return one note by id'


class StubSettler:
    def __init__(self, check, result: SettlementResult):
        self.check, self.result, self.calls = check, result, 0

    def __call__(self, head_dir: Path, target) -> SettlementResult:
        self.calls += 1
        assert Path(target.target.holdouts_path).is_absolute()
        assert Path(target.target.holdouts_path).is_dir()
        self.check((head_dir / APP).read_text())
        return self.result


def bug_landed(src: str) -> None:
    assert src.count(ADMIN_DEP) == (TARGET / APP).read_text().count(ADMIN_DEP) - 1


def docstring_landed(src: str) -> None:
    assert DOCSTRING in src and DOCSTRING not in (TARGET / APP).read_text()


def deps_for(tmp_path, settler, seed):
    cfg = ServerConfig()
    sf = make_session_factory("sqlite://")
    engine = MarketEngine(sf, cfg, EngineProvider(FakeProvider()), rng=random.Random(seed))  # noqa: S311
    return PipelineDeps(
        RuntimeEnv(cache_dir=tmp_path / "cache", fake_llm=True), cfg, sf, engine, None, settler
    )


@pytest.mark.parametrize("seed", [0, 1, 2])
async def test_bug_auth_is_blocked_and_settles_no(tmp_path, seed):
    settler = StubSettler(bug_landed, SettlementResult("NO", 10, 12, [], 5, None))
    deps = deps_for(tmp_path, settler, seed)
    statuses = []
    mid = await run_local_market(
        TARGET, PATCHES / "bug_auth.patch", repo="local/sample-target", deps=deps, statuses=statuses
    )
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
    assert settler.calls == 1
    assert positions and all(p.payout is not None for p in positions)
    assert sum(p.payout for p in positions) == pytest.approx(sum(p.no_shares for p in positions))
    assert statuses[-1][1].endswith("· settled NO · market right")
    assert [s for s, _, _ in statuses][:2] == ["pending", "failure"]


@pytest.mark.parametrize("seed", [0, 1, 2])
async def test_good_docstring_auto_merges_and_settles_yes(tmp_path, seed):
    settler = StubSettler(docstring_landed, SettlementResult("YES", 12, 12, [], 5, None))
    deps = deps_for(tmp_path, settler, seed)
    mid = await run_local_market(
        TARGET, PATCHES / "good_docstring.patch", repo="local/sample-target", deps=deps
    )
    with deps.session_factory() as s:
        m = s.get(Market, mid)
    assert m.final_price >= 0.90
    assert (m.gate_state, m.state, m.outcome, m.resolution) == ("success", "SETTLED", "YES", "paid")
    assert deps.engine.status_description(mid).endswith("· settled YES · market right")


async def test_every_demo_run_opens_a_fresh_market(tmp_path):
    settler = StubSettler(docstring_landed, SettlementResult("YES", 12, 12, [], 5, None))
    deps = deps_for(tmp_path, settler, 0)
    patch = PATCHES / "good_docstring.patch"
    a = await run_local_market(TARGET, patch, repo="local/sample-target", deps=deps)
    b = await run_local_market(TARGET, patch, repo="local/sample-target", deps=deps)
    assert a != b
    assert not list((tmp_path / "cache").iterdir())  # temp copies cleaned up
