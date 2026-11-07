"""Post-merge settlement on the server (DECISIONS #73): the merge webhook records the merge on a GATED GitHub
market, and the admin "fast-forward" settles it with the post-merge oracle as of the window's end, then posts
the final `pr-market` status. The oracle and GitHub are faked; no network."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from prm.config import RuntimeEnv, ServerConfig, TargetGateOverrides
from prm.models import Market
from prm.server import create_app
from prm.types import SettlementResult
from tests.test_server import ADMIN, HOOK_KEY, REPO, admin_headers, deliver, engine_for, register
from tests.traders._bundles import GOOD_DIFF, make_bundle

HEAD, BASE, MERGE = "a" * 40, "b" * 40, "c" * 40
MERGED_AT = datetime(2026, 11, 7, 21, 0, tzinfo=UTC)
TOML = '[target]\nname = "widgets"\n[settle]\ncommand = "true"\n[postmerge]\nwindow_days = 3\n'


class FakeGitHub:
    def __init__(self, toml: str | None = TOML) -> None:
        self.toml, self.statuses = toml, []

    async def get_file(self, repo, path, ref):
        assert (repo, path, ref) == (REPO, ".prm.toml", BASE)  # always the base sha
        return self.toml

    async def post_status(self, repo, sha, state, description, target_url=None):
        self.statuses.append((repo, sha, state, description, target_url))


class FakeSettle:
    def __init__(self, result: SettlementResult | None) -> None:
        self.result, self.calls = result, []

    def __call__(self, full_name, pr, **kw):
        self.calls.append((full_name, pr, kw))
        return self.result


YES = SettlementResult("YES", 0, 0, [], 5, None)


def make_client(monkeypatch, gh, settle):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", HOOK_KEY)
    monkeypatch.setenv("PRM_ADMIN_TOKEN", ADMIN)
    env = RuntimeEnv(database_url_override="sqlite://", public_url="https://prm.example")

    async def no_run(*_a):
        return None

    return TestClient(create_app(env, ServerConfig(), run_market=no_run, github=gh, settle_fn=settle))


async def gated_market(app, tmp_path) -> int:
    eng = engine_for(app)
    bundle = replace(make_bundle(tmp_path, diff=GOOD_DIFF), source="github", repo=REPO, pr_number=7,
                     head_sha=HEAD, base_sha=BASE)  # fmt: skip
    mid = eng.create(eng.ensure_repo(REPO), bundle)
    await eng.run_agents(mid, bundle)
    eng.close(mid)
    eng.gate(mid, TargetGateOverrides())
    return mid


def merged(**over):
    pr = {
        "number": 7, "merged": True, "merged_at": "2026-11-07T21:00:00Z", "merge_commit_sha": MERGE,
        "head": {"sha": HEAD, "repo": {"full_name": REPO}}, "base": {"sha": BASE, "ref": "main"},
    }  # fmt: skip
    pr.update(over)
    return {"action": "closed", "pull_request": pr, "repository": {"full_name": REPO}}


def market(app, mid: int) -> Market:
    with app.state.session_factory() as s:
        return s.get_one(Market, mid)


async def test_merge_then_fast_forward_settles_and_posts_the_final_status(monkeypatch, tmp_path):
    gh, oracle = FakeGitHub(), FakeSettle(YES)
    with make_client(monkeypatch, gh, oracle) as client:
        register(client)
        mid = await gated_market(client.app, tmp_path)
        r = deliver(client, merged())
        assert r.status_code == 202 and r.json() == {"merge_recorded": {"market_id": mid}}
        assert client.get(f"/api/markets/{mid}").json()["merge_sha"] == MERGE

        r = client.post(f"/api/markets/{mid}/fast-forward", headers=admin_headers())
        assert r.status_code == 200, r.text
        body = r.json()
        assert (body["state"], body["outcome"], body["resolution"], body["window_days"]) == (
            "SETTLED",
            "YES",
            "paid",
            3,
        )
        assert "settled YES" in body["status_description"]

        ((full_name, pr, kw),) = oracle.calls
        assert (full_name, pr.merge_sha, pr.merged_at, kw["branch"]) == (REPO, MERGE, MERGED_AT, "main")
        assert kw["now"] == MERGED_AT + timedelta(days=3) and kw["window_days"] == 3
        assert gh.statuses == [
            (REPO, HEAD, "success", body["status_description"], f"https://prm.example/m/{mid}")
        ]
        assert client.post(f"/api/markets/{mid}/fast-forward", headers=admin_headers()).status_code == 409


async def test_fast_forward_needs_a_recorded_merge_and_the_admin_token(monkeypatch, tmp_path):
    with make_client(monkeypatch, FakeGitHub(), FakeSettle(YES)) as client:
        register(client)
        mid = await gated_market(client.app, tmp_path)
        assert client.post(f"/api/markets/{mid}/fast-forward").status_code == 401
        assert client.post(f"/api/markets/{mid}/fast-forward", headers=admin_headers()).status_code == 409
        assert client.post("/api/markets/999/fast-forward", headers=admin_headers()).status_code == 404


async def test_merges_without_a_gated_market_are_ignored(monkeypatch, tmp_path):
    with make_client(monkeypatch, FakeGitHub(), FakeSettle(YES)) as client:
        register(client)
        r = deliver(client, merged())
        assert r.json() == {"ignored": "no gated market for this merge"}
        r = deliver(client, merged(merged=False))
        assert "ignored" in r.json()


@pytest.mark.parametrize("toml", [None, "not toml ["])
async def test_unreadable_base_config_falls_back_to_14_days(monkeypatch, tmp_path, toml):
    oracle = FakeSettle(YES)
    with make_client(monkeypatch, FakeGitHub(toml), oracle) as client:
        register(client)
        mid = await gated_market(client.app, tmp_path)
        deliver(client, merged())
        assert (
            client.post(f"/api/markets/{mid}/fast-forward", headers=admin_headers()).json()["window_days"]
            == 14
        )
        assert oracle.calls[0][2]["now"] == MERGED_AT + timedelta(days=14)


async def test_an_oracle_infra_error_voids_and_still_reports(monkeypatch, tmp_path):
    gh = FakeGitHub()
    with make_client(monkeypatch, gh, FakeSettle(SettlementResult.infra_error("git down"))) as client:
        register(client)
        mid = await gated_market(client.app, tmp_path)
        deliver(client, merged())
        r = client.post(f"/api/markets/{mid}/fast-forward", headers=admin_headers())
        assert r.status_code == 200 and r.json()["state"] == "VOID"
        assert market(client.app, mid).resolution == "void"
        assert gh.statuses[0][2] == "failure"
