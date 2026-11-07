"""WP J: judge (demo) markets and judge betting (R-UI-2, PLAN §4). A real engine on a temp SQLite file, the
fake LLM, a small pool of sample-target mutants and an injected oracle (one test runs the real hidden
tests)."""

from __future__ import annotations

import random
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from prm.config import RuntimeEnv, ServerConfig, TargetConfig
from prm.db import make_session_factory
from prm.demo import MAX_DIFF_LINES, DemoConflict, DemoUnavailable, JudgeDemo, changed_lines, diff_sha
from prm.market.engine import MarketEngine
from prm.models import Market
from prm.server import JUDGE_COOKIE, RateLimiter, create_app, sign_judge, verify_judge
from prm.traders.fake import FakeProvider
from prm.traders.roster import EngineProvider
from prm.types import SettlementResult, State
from tests.league.sample import copy_sample

ADMIN = "admin-test-value"
AUTH = {"Authorization": f"Bearer {ADMIN}"}


class FixedOracle:
    def __init__(self, outcome: str = "NO") -> None:
        self.outcome, self.calls = outcome, []

    def __call__(self, head_dir: Path, target: TargetConfig, root: Path) -> SettlementResult:
        self.calls.append(head_dir)
        return SettlementResult(self.outcome, 3, 5, [], 1, None)


@pytest.fixture
def env(tmp_path: Path) -> RuntimeEnv:
    return RuntimeEnv(
        database_url_override=f"sqlite:///{tmp_path / 'prm.db'}",
        cache_dir=tmp_path / "cache",
        public_url="https://prm.example",
    )


def make_demo(env: RuntimeEnv, target: Path | None, oracle=None, pool_size: int = 6) -> JudgeDemo:
    sf = make_session_factory(env)
    engine = MarketEngine(sf, ServerConfig(), EngineProvider(FakeProvider()), rng=random.Random(1))  # noqa: S311
    kwargs = {"oracle": oracle} if oracle is not None else {}
    return JudgeDemo(
        engine,
        sf,
        env,
        target_dir=target,
        pool_size=pool_size,
        rng=random.Random(2),  # noqa: S311
        **kwargs,
    )


@pytest.fixture
def target(tmp_path: Path) -> Path:
    return copy_sample(tmp_path / "targets")


@pytest.fixture
def oracle() -> FixedOracle:
    return FixedOracle("NO")


@pytest.fixture
def demo(env, target, oracle) -> JudgeDemo:
    return make_demo(env, target, oracle)


@pytest.fixture
def client(env, demo, monkeypatch):
    monkeypatch.setenv("PRM_ADMIN_TOKEN", ADMIN)
    with TestClient(create_app(env, ServerConfig(), demo=demo)) as c:
        yield c


def join(client: TestClient, name: str = "Ada") -> dict[str, str]:
    r = client.post("/api/judges", json={"name": name})
    assert r.status_code == 201, r.text
    return {"Cookie": f"{JUDGE_COOKIE}={r.cookies[JUDGE_COOKIE]}"}


def state(demo: JudgeDemo, market_id: int) -> State:
    with demo._sf() as s:
        return State(s.get_one(Market, market_id).state)


# ------------------------------------------------------------------------------------------------ service


def test_changed_lines_ignores_headers():
    diff = "--- a/x.py\n+++ b/x.py\n@@ -1,2 +1,2 @@\n-a\n+b\n ctx\n"
    assert changed_lines(diff) == 2


async def test_open_draws_a_small_mutant_then_agents_hand_over_to_judges(demo):
    market_id, bundle = demo.open()
    assert bundle.source == "demo" and bundle.repo == "demo/sample-target"
    assert 0 < changed_lines(bundle.diff) < MAX_DIFF_LINES
    assert bundle.head_sha == diff_sha(bundle.diff)
    assert state(demo, market_id) == State.CONTEXT_READY
    await demo.run_agents(market_id, bundle)
    assert state(demo, market_id) == State.HUMAN_WINDOW


async def test_one_open_judge_market_at_a_time(demo):
    market_id, bundle = demo.open()
    with pytest.raises(DemoConflict):
        demo.open()
    await demo.run_agents(market_id, bundle)
    await demo.close_and_settle(market_id)
    assert demo.open()[0] != market_id


async def test_close_and_settle_runs_the_oracle_on_the_mutant_and_pays_out(demo, oracle):
    market_id, bundle = demo.open()
    await demo.run_agents(market_id, bundle)
    payouts = await demo.close_and_settle(market_id)
    assert oracle.calls == [bundle.head_dir]
    with demo._sf() as s:
        m = s.get_one(Market, market_id)
        assert (m.state, m.outcome, m.resolution) == ("SETTLED", "NO", "paid")  # pays even if gated
    assert set(payouts) == {t.name for t in ServerConfig().traders}


async def test_close_and_settle_refuses_outside_the_human_window(demo):
    market_id, _ = demo.open()
    with pytest.raises(DemoConflict):
        await demo.close_and_settle(market_id)  # agents haven't traded yet
    with pytest.raises(DemoConflict):
        await demo.close_and_settle(9999)


async def test_an_oracle_crash_voids_and_refunds(env, target):
    def boom(*_args):
        raise RuntimeError("sandbox down")

    demo = make_demo(env, target, boom)
    market_id, bundle = demo.open()
    await demo.run_agents(market_id, bundle)
    assert await demo.close_and_settle(market_id) == {}
    with demo._sf() as s:
        m = s.get_one(Market, market_id)
        assert (m.state, m.gate_state, m.resolution) == ("VOID", "failure", "void")


async def test_the_pool_is_found_again_after_a_restart(env, target, oracle):
    market_id, bundle = make_demo(env, target, oracle).open()
    restarted = make_demo(env, target, oracle)  # same cache dir and seed: same pool
    await restarted.run_agents(market_id, bundle)
    await restarted.close_and_settle(market_id)
    assert oracle.calls == [bundle.head_dir]


def test_unavailable_without_a_target_or_hidden_tests(env, target, monkeypatch):
    monkeypatch.delenv("PRM_DEMO_TARGET", raising=False)
    with pytest.raises(DemoUnavailable):
        make_demo(env, None).open()
    toml = target / ".prm.toml"
    toml.write_text(toml.read_text().replace('holdouts_path = "../sample-target-holdouts"\n', ""))
    with pytest.raises(DemoUnavailable):
        make_demo(env, target).open()


async def test_real_hidden_tests_settle_a_judge_market(env, target):
    demo = make_demo(env, target, pool_size=4)
    market_id, bundle = demo.open()
    await demo.run_agents(market_id, bundle)
    await demo.close_and_settle(market_id)
    with demo._sf() as s:
        m = s.get_one(Market, market_id)
        assert m.state == "SETTLED" and m.outcome in ("YES", "NO") and m.holdouts_total > 0


# -------------------------------------------------------------------------------------------- judge cookie


def test_judge_cookie_signature():
    key = b"k" * 32
    good = sign_judge(7, key)
    assert verify_judge(good, key) == 7
    for bad in ("", "7", "7.", "07" + good[1:], good[:-1] + "0", sign_judge(7, b"x" * 32), "x.y", "-1.ab"):
        assert verify_judge(bad, key) is None


def test_rate_limiter_window():
    now = [0.0]
    rl = RateLimiter(2, 60.0, clock=lambda: now[0])
    assert rl.allow("a") and rl.allow("a") and not rl.allow("a") and rl.allow("b")
    now[0] = 60.1
    assert rl.allow("a")


# -------------------------------------------------------------------------------------------------- API


def test_full_judge_flow(client, demo):
    assert client.get("/api/demo/current").json() == {"market": None}
    r = client.post("/api/demo/markets", headers=AUTH)
    assert r.status_code == 202, r.text
    market_id = r.json()["market_id"]
    assert client.get("/api/demo/current").json()["market"]["state"] == "HUMAN_WINDOW"  # agents ran

    ada = join(client, "Ada")
    me = client.get("/api/judges/me", headers=ada).json()
    assert (me["name"], me["bankroll"], me["next_cap"], me["positions"]) == ("Ada", 100.0, 25.0, [])
    r = client.post(f"/api/markets/{market_id}/human-trades", json={"side": "NO", "spend": 10}, headers=ada)
    assert r.status_code == 200, r.text
    assert r.json()["price_after"] < r.json()["price_before"]
    r = client.post(f"/api/markets/{market_id}/human-trades", json={"side": "NO", "spend": 16}, headers=ada)
    assert r.status_code == 422  # same cap as agents: 25 per market

    r = client.post(f"/api/demo/markets/{market_id}/close-and-settle", headers=AUTH)
    assert r.status_code == 200, r.text
    assert (r.json()["state"], r.json()["outcome"]) == ("SETTLED", "NO")
    assert r.json()["payouts"]["Ada"] > 10
    me = client.get("/api/judges/me", headers=ada).json()
    assert me["pnl"] > 0 and me["positions"][0]["state"] == "SETTLED" and me["bankroll"] > 100

    board = client.get("/api/leaderboard", params={"repo": "demo/sample-target"}).json()["traders"]
    assert {t["kind"] for t in board} == {"agent", "human"}
    r = client.post(f"/api/markets/{market_id}/human-trades", json={"side": "YES", "spend": 1}, headers=ada)
    assert r.status_code == 409  # betting closed


def test_demo_admin_routes_need_the_token(client):
    assert client.post("/api/demo/markets").status_code == 401
    assert (
        client.post("/api/demo/markets/1/close-and-settle", headers={"Authorization": "Bearer x"}).status_code
        == 401
    )


def test_second_open_is_a_conflict_and_bad_close_is_409(client):
    market_id = client.post("/api/demo/markets", headers=AUTH).json()["market_id"]
    assert client.post("/api/demo/markets", headers=AUTH).status_code == 409
    client.post(f"/api/demo/markets/{market_id}/close-and-settle", headers=AUTH)
    assert client.post(f"/api/demo/markets/{market_id}/close-and-settle", headers=AUTH).status_code == 409


def test_unconfigured_demo_is_503(env, monkeypatch):
    monkeypatch.setenv("PRM_ADMIN_TOKEN", ADMIN)
    monkeypatch.delenv("PRM_DEMO_TARGET", raising=False)
    with TestClient(create_app(env, ServerConfig(), demo=make_demo(env, None))) as c:
        assert c.post("/api/demo/markets", headers=AUTH).status_code == 503


def test_judge_routes_reject_missing_or_forged_cookies(client):
    assert client.get("/api/judges/me").status_code == 401
    forged = {"Cookie": f"{JUDGE_COOKIE}={sign_judge(1, b'wrong-key')}"}
    assert client.get("/api/judges/me", headers=forged).status_code == 401
    r = client.post("/api/markets/1/human-trades", json={"side": "YES", "spend": 1}, headers=forged)
    assert r.status_code == 401


def test_judges_fail_closed_without_the_admin_token(client, monkeypatch):
    monkeypatch.delenv("PRM_ADMIN_TOKEN")
    assert client.post("/api/judges", json={"name": "Ada"}).status_code == 503


def test_judge_names_are_validated_and_deduplicated(client):
    for bad in ("", "   ", "x" * 33, "a\x00b"):
        assert client.post("/api/judges", json={"name": bad}).status_code == 422
    assert client.post("/api/judges", json={"name": "Ada", "bankroll": 1e9}).status_code == 422
    names = [client.post("/api/judges", json={"name": " Ada  L "}).json()["name"] for _ in range(3)]
    assert names == ["Ada L", "Ada L 2", "Ada L 3"]


def test_join_is_rate_limited_per_ip(client):
    codes = [client.post("/api/judges", json={"name": f"j{i}"}).status_code for i in range(11)]
    assert codes == [201] * 10 + [429]


def test_join_sets_a_hardened_cookie(client):
    cookie = client.post("/api/judges", json={"name": "Ada"}).headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "secure" in cookie


@pytest.mark.parametrize(
    "body", [{"side": "MAYBE", "spend": 1}, {"side": "YES", "spend": 0}, {"side": "YES"}]
)
def test_human_trade_body_is_validated(client, body):
    market_id = client.post("/api/demo/markets", headers=AUTH).json()["market_id"]
    r = client.post(f"/api/markets/{market_id}/human-trades", json=body, headers=join(client))
    assert r.status_code == 422


def test_judges_cannot_bet_on_real_or_missing_markets(client, demo):
    ada = join(client)
    assert (
        client.post(
            "/api/markets/404/human-trades", json={"side": "YES", "spend": 1}, headers=ada
        ).status_code
        == 404
    )
    with demo._sf.begin() as s:
        s.get_one(
            Market, client.post("/api/demo/markets", headers=AUTH).json()["market_id"]
        ).source = "github"
    r = client.post("/api/markets/1/human-trades", json={"side": "YES", "spend": 1}, headers=ada)
    assert r.status_code == 409
