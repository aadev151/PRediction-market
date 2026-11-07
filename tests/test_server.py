"""The FastAPI server (PLAN §4): auth on every route, the webhook, the reveal rule, counts-only settlement,
metrics, QR path validation and the middleware. No network: TestClient and in-memory SQLite."""

import hashlib
import hmac
import json
import logging
import random
from dataclasses import replace

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from prm.config import RuntimeEnv, ServerConfig, TargetGateOverrides
from prm.market.engine import MarketEngine
from prm.models import LeagueRun
from prm.server import (
    AUTH_DECLARATIONS,
    admin_token,
    create_app,
    github_hmac,
    judge_cookie,
    public,
    route_auth,
    undeclared_routes,
)
from prm.traders.fake import FakeProvider
from prm.traders.roster import EngineProvider
from prm.types import Finding, SettlementResult
from tests.traders._bundles import BUG_DIFF, GOOD_DIFF, make_bundle

HOOK_KEY = "hook-test-key"
ADMIN = "admin-test-value"
REPO = "acme/widgets"
HEAD, BASE = "a" * 40, "b" * 40


@pytest.fixture
def calls():
    return []


@pytest.fixture
def app(calls, monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", HOOK_KEY)
    monkeypatch.setenv("PRM_ADMIN_TOKEN", ADMIN)

    async def fake_run_market(repo: str, pr: int, sha: str) -> None:
        calls.append((repo, pr, sha))

    env = RuntimeEnv(database_url_override="sqlite://", public_url="https://prm.example")
    return create_app(env, ServerConfig(), run_market=fake_run_market)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


def admin_headers(value: str = ADMIN) -> dict[str, str]:
    return {"Authorization": f"Bearer {value}"}


def register(client: TestClient, name: str = REPO) -> None:
    assert client.post("/api/repos", json={"full_name": name}, headers=admin_headers()).status_code in (
        200,
        201,
    )


def pr_payload(action: str = "opened", repo: str = REPO, **pr_overrides) -> dict:
    pr = {
        "number": 7,
        "draft": False,
        "head": {"sha": HEAD, "repo": {"full_name": repo}},
        "base": {"sha": BASE},
    }
    pr.update(pr_overrides)
    return {"action": action, "pull_request": pr, "repository": {"full_name": repo}}


def deliver(client: TestClient, payload: object, event: str = "pull_request", key: str = HOOK_KEY, sig=True):
    body = json.dumps(payload).encode()
    headers = {"X-GitHub-Event": event, "Content-Type": "application/json"}
    if sig:
        headers["X-Hub-Signature-256"] = "sha256=" + hmac.new(key.encode(), body, hashlib.sha256).hexdigest()
    return client.post("/api/hooks/github", content=body, headers=headers)


def engine_for(app: FastAPI) -> MarketEngine:
    sf = app.state.session_factory
    return MarketEngine(sf, ServerConfig(), EngineProvider(FakeProvider()), rng=random.Random(1))  # noqa: S311


async def open_market(app, tmp_path, *, diff=GOOD_DIFF, source="github", pr=7, sha="feedface01"):
    eng = engine_for(app)
    bundle = replace(make_bundle(tmp_path, diff=diff), source=source, repo=REPO, pr_number=pr, head_sha=sha)
    mid = eng.create(eng.ensure_repo(REPO), bundle)
    await eng.run_agents(mid, bundle)
    return eng, mid


def settle(eng: MarketEngine, mid: int, outcome: str = "YES") -> float:
    price = eng.close(mid)
    eng.gate(mid, TargetGateOverrides())
    finding = Finding(id="RT-internal-7", title="weak token check", severity="high", confirmed=True)
    eng.settle(mid, SettlementResult(outcome, 4, 5, [finding], 12, None))
    return price


# ------------------------------------------------------------------------------------------------ auth


def test_every_route_declares_exactly_one_auth(app):
    assert undeclared_routes(app) == []
    expected = {
        ("POST", "/api/repos"): admin_token,
        ("POST", "/api/hooks/github"): github_hmac,
        ("POST", "/api/demo/markets"): admin_token,
        ("POST", "/api/demo/markets/{market_id}/close-and-settle"): admin_token,
        ("GET", "/api/judges/me"): judge_cookie,
        ("POST", "/api/markets/{market_id}/human-trades"): judge_cookie,
    }
    for route in app.routes:
        if isinstance(route, APIRoute):
            for method in route.methods:
                assert route_auth(route) == [expected.get((method, route.path), public)], route.path


def test_an_undeclared_route_is_detected(app):
    app.add_api_route("/sneaky", lambda: {}, methods=["GET"])
    assert undeclared_routes(app) == ["['GET'] /sneaky"]
    assert len(AUTH_DECLARATIONS) == 4


def test_admin_auth_fails_closed_without_env(client, monkeypatch):
    monkeypatch.delenv("PRM_ADMIN_TOKEN")
    r = client.post("/api/repos", json={"full_name": REPO}, headers=admin_headers())
    assert r.status_code == 503


@pytest.mark.parametrize(
    "headers", [{}, admin_headers("wrong"), {"Authorization": ADMIN}, {"Authorization": "Basic x"}]
)
def test_admin_auth_rejects_wrong_or_missing(client, headers):
    assert client.post("/api/repos", json={"full_name": REPO}, headers=headers).status_code == 401
    assert client.post("/api/repos", json={"bad": 1}, headers=headers).status_code == 401  # auth first


def test_add_repo_creates_once_and_validates(client):
    r = client.post("/api/repos", json={"full_name": REPO}, headers=admin_headers())
    assert r.status_code == 201 and r.json()["full_name"] == REPO
    again = client.post("/api/repos", json={"full_name": "ACME/widgets"}, headers=admin_headers())
    assert again.status_code == 200 and again.json()["id"] == r.json()["id"]
    for bad in ["widgets", "a/b/c", "../x", "acme/..", "acme/wid gets"]:
        assert client.post("/api/repos", json={"full_name": bad}, headers=admin_headers()).status_code == 422
    assert [x["full_name"] for x in client.get("/api/repos").json()["repos"]] == [REPO]
    board = client.get("/api/leaderboard", params={"repo": REPO}).json()["traders"]
    assert {t["name"] for t in board} == {"diff_reader", "coverage", "security", "skeptic"}
    assert all(t["bankroll"] == 100.0 and t["markets_traded"] == 0 and t["kind"] == "agent" for t in board)


# --------------------------------------------------------------------------------------------- webhook


def test_webhook_fails_closed_without_secret(client, calls, monkeypatch):
    monkeypatch.delenv("GITHUB_WEBHOOK_SECRET")
    register(client)
    assert deliver(client, pr_payload()).status_code == 503
    assert calls == []


def test_webhook_rejects_bad_or_missing_signature(client, calls):
    register(client)
    assert deliver(client, pr_payload(), key="other-key").status_code == 401
    assert deliver(client, pr_payload(), sig=False).status_code == 401
    assert calls == []


def test_webhook_ping(client):
    r = deliver(client, {"zen": "Keep it logically awesome.", "hook_id": 1}, event="ping")
    assert r.status_code == 200


def test_webhook_schedules_run_market_with_exact_args(client, calls):
    register(client)
    r = deliver(client, pr_payload("synchronize"))
    assert r.status_code == 202
    assert r.json() == {"scheduled": {"repo": REPO, "pr_number": 7, "head_sha": HEAD}}
    assert calls == [(REPO, 7, HEAD)]


def test_webhook_passes_the_registered_repo_name(client, calls):
    register(client, "Acme/Widgets")
    assert deliver(client, pr_payload(repo="acme/widgets")).status_code == 202
    assert calls == [("Acme/Widgets", 7, HEAD)]


@pytest.mark.parametrize(
    ("payload", "event", "reason"),
    [
        (pr_payload("closed"), "pull_request", "action not handled"),
        (pr_payload(draft=True), "pull_request", "draft pull request"),
        (pr_payload(head={"sha": HEAD, "repo": {"full_name": "mallory/widgets"}}), "pull_request", "fork"),
        (pr_payload(head={"sha": HEAD, "repo": None}), "pull_request", "fork pull request"),
        (pr_payload(), "push", "event not handled"),
        (pr_payload(repo="acme/other"), "pull_request", "repo not registered"),
    ],
)
def test_webhook_ignores_non_matching_events(client, calls, payload, event, reason):
    register(client)
    r = deliver(client, payload, event=event)
    assert r.status_code == 202 and r.json()["ignored"].startswith(reason)
    assert calls == []


def test_webhook_dedupes_redeliveries(client, calls):
    register(client)
    assert deliver(client, pr_payload("opened")).json()["scheduled"]["head_sha"] == HEAD
    assert deliver(client, pr_payload("reopened")).json() == {"ignored": "duplicate delivery"}
    new_sha = pr_payload("synchronize", head={"sha": "c" * 40, "repo": {"full_name": REPO}})
    assert deliver(client, new_sha).status_code == 202
    assert calls == [(REPO, 7, HEAD), (REPO, 7, "c" * 40)]


def test_webhook_survives_a_failing_pipeline_and_allows_retry(monkeypatch):
    monkeypatch.setenv("GITHUB_WEBHOOK_SECRET", HOOK_KEY)
    monkeypatch.setenv("PRM_ADMIN_TOKEN", ADMIN)
    attempts = []

    async def broken(repo: str, pr: int, sha: str) -> None:
        attempts.append(sha)
        raise RuntimeError("boom")

    app = create_app(RuntimeEnv(database_url_override="sqlite://"), ServerConfig(), run_market=broken)
    with TestClient(app) as c:
        register(c)
        assert deliver(c, pr_payload()).status_code == 202
        assert deliver(c, pr_payload()).status_code == 202
    assert attempts == [HEAD, HEAD]


def test_webhook_rejects_signed_non_json(client):
    body = b"payload=%7B%7D"
    sig = "sha256=" + hmac.new(HOOK_KEY.encode(), body, hashlib.sha256).hexdigest()
    headers = {"X-GitHub-Event": "pull_request", "X-Hub-Signature-256": sig}
    assert client.post("/api/hooks/github", content=body, headers=headers).status_code == 400


# ------------------------------------------------------------------------------------- markets: reveal


async def test_beliefs_hidden_while_trading(app, client, tmp_path):
    _eng, mid = await open_market(app, tmp_path)
    r = client.get(f"/api/markets/{mid}")
    body = r.json()
    assert r.status_code == 200 and body["state"] == "TRADING"
    assert body["beliefs_revealed"] is False and body["beliefs"] is None
    assert body["trades"] and {"trader", "side", "escalation", "round"} <= set(body["trades"][0])
    assert "rationale" not in r.text and "p_yes" not in r.text and "key_risks" not in r.text
    listed = client.get("/api/markets").json()["markets"]
    assert [m["id"] for m in listed] == [mid] and "beliefs" not in listed[0]
    assert listed[0]["final_price"] is None and 0 < listed[0]["price"] < 1


async def test_beliefs_revealed_after_close(app, client, tmp_path):
    eng, mid = await open_market(app, tmp_path)
    eng.close(mid)
    body = client.get(f"/api/markets/{mid}").json()
    assert body["state"] == "CLOSED" and body["beliefs_revealed"] is True
    assert len(body["beliefs"]) == 4
    assert {"trader", "p_yes", "rationale", "key_risks", "escalation"} <= set(body["beliefs"][0])


async def test_settlement_is_counts_only(app, client, tmp_path):
    eng, mid = await open_market(app, tmp_path)
    settle(eng, mid, "YES")
    r = client.get(f"/api/markets/{mid}")
    body = r.json()
    assert body["state"] == "SETTLED" and body["resolution"] == "paid" and body["outcome"] == "YES"
    assert body["settlement"] == {
        "holdouts_passed": 4,
        "holdouts_total": 5,
        "findings": [{"title": "weak token check", "severity": "high"}],
    }
    assert "RT-internal-7" not in r.text and "confirmed" not in r.text and "meta" not in body
    assert body["status_description"].endswith("settled YES · market right")
    assert [e["to_state"] for e in body["events"]][-1] == "SETTLED"
    assert body["gate"]["state"] == "success"


async def test_market_list_filters_and_404(app, client, tmp_path):
    await open_market(app, tmp_path)
    assert len(client.get("/api/markets", params={"repo": REPO}).json()["markets"]) == 1
    assert client.get("/api/markets", params={"repo": "acme/none"}).json()["markets"] == []
    assert client.get("/api/markets", params={"limit": 0}).status_code == 422
    assert client.get("/api/markets/999").status_code == 404


# -------------------------------------------------------------------------------- metrics, leaderboard


async def test_metrics_brier_counts_only_real_prs_and_shows_n(app, client, tmp_path):
    empty = client.get("/api/metrics").json()
    assert empty["real_pr_n"] == 0 and empty["real_pr_brier"] is None and empty["errors"] == 0
    eng, good = await open_market(app, tmp_path, pr=1, sha="good01")
    p = settle(eng, good, "YES")
    eng2, other = await open_market(app, tmp_path, source="league", pr=2, sha="lg01")
    settle(eng2, other, "NO")
    eng3, broken = await open_market(app, tmp_path, pr=3, sha="void01")
    eng3.void(broken, "test infra error")
    m = client.get("/api/metrics").json()
    assert m["real_pr_n"] == 1 and m["real_pr_brier"] == round((p - 1.0) ** 2, 4)
    assert m["errors"] == 1 and m["markets_by_state"]["VOID"] == 1 and m["markets_by_state"]["SETTLED"] == 2
    assert m["markets_today"] == 3 and m["llm_usd_today"] >= 0 and m["real_prs_priced"] == 1


async def test_refunded_market_is_not_an_error_and_leaderboard_pnl(app, client, tmp_path):
    eng, mid = await open_market(app, tmp_path, diff=BUG_DIFF)
    settle(eng, mid, "NO")
    body = client.get(f"/api/markets/{mid}").json()
    assert body["state"] == "VOID" and body["resolution"] == "refunded" and body["gate"]["state"] == "failure"
    assert client.get("/api/metrics").json()["errors"] == 0
    board = client.get("/api/leaderboard").json()["traders"]
    assert all(t["pnl"] == 0.0 and t["bankroll"] == pytest.approx(100.0) for t in board)
    assert any(t["markets_traded"] == 1 for t in board)


def test_league_report_404_then_latest(app, client):
    r = client.get("/api/league/report")
    assert r.status_code == 404 and r.json() == {"detail": "no league run yet"}
    with app.state.session_factory.begin() as s:
        s.add(LeagueRun(repo_label="x", report_json=json.dumps({"n": 1})))
        s.add(LeagueRun(repo_label="x", report_json=json.dumps({"n": 2})))
        s.add(LeagueRun(repo_label="x", report_json=None))
    assert client.get("/api/league/report").json() == {"n": 2}


# --------------------------------------------------------------------------------------- QR and pages


def test_qr_svg_for_public_url(client):
    r = client.get("/api/qr.svg", params={"path": "/judge"})
    assert r.status_code == 200 and r.headers["content-type"].startswith("image/svg+xml")
    assert r.text.startswith("<svg")


@pytest.mark.parametrize(
    "path", ["//evil.example", "https://evil.example", "judge", "/\\evil.example", "/a\nb"]
)
def test_qr_rejects_unsafe_paths(client, path):
    assert client.get("/api/qr.svg", params={"path": path}).status_code == 422


@pytest.mark.parametrize("path", ["/", "/m/1", "/league", "/how", "/judge", "/live"])
def test_pages_serve_html(client, path):
    r = client.get(path, params={"name": "../../pyproject"})  # a query can't pick the file
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/html")
    assert "[project]" not in r.text


def test_market_page_needs_an_integer_id(client):
    assert client.get("/m/abc").status_code == 422


# ------------------------------------------------------------------------------------------ middleware


def test_security_headers_and_generated_request_id(client):
    r = client.get("/healthz")
    assert r.json() == {"status": "ok"}
    assert r.headers["X-Content-Type-Options"] == "nosniff"
    assert r.headers["X-Frame-Options"] == "DENY"
    assert r.headers["Referrer-Policy"] == "no-referrer"
    assert "frame-ancestors 'none'" in r.headers["Content-Security-Policy"]
    assert len(r.headers["X-Request-ID"]) == 32
    assert client.get("/api/markets/999").headers["X-Frame-Options"] == "DENY"  # error responses too


def test_request_id_is_honored_only_when_well_formed(client):
    assert client.get("/healthz", headers={"X-Request-ID": "abc-123"}).headers["X-Request-ID"] == "abc-123"
    assert client.get("/healthz", headers={"X-Request-ID": "bad id!"}).headers["X-Request-ID"] != "bad id!"


def test_access_log_is_one_json_line_without_query(client):
    lines: list[str] = []

    class Grab(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            lines.append(record.getMessage())

    handler = Grab()
    logging.getLogger("prm.access").addHandler(handler)
    try:
        client.get("/api/qr.svg", params={"path": "/judge", "token": "do-not-log"})
    finally:
        logging.getLogger("prm.access").removeHandler(handler)
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["path"] == "/api/qr.svg" and entry["status"] == 200 and entry["method"] == "GET"
    assert "do-not-log" not in lines[0] and {"ms", "request_id"} <= set(entry)
