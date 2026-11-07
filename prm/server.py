"""WP E. FastAPI app: API routes (each declares its auth) + dashboard. PLAN §4.

Auth is declarative: every route carries exactly one of the dependencies in ``AUTH_DECLARATIONS``
(``public``, ``admin_token``, ``github_hmac``). ``create_app`` refuses to build an app with an undeclared
route, and ``tests/test_server.py`` checks the same thing. Secrets (``PRM_ADMIN_TOKEN``,
``GITHUB_WEBHOOK_SECRET``) are read from the environment on every request and never logged; if one is unset
its routes answer 503 (fail closed).

The market detail reveals beliefs and rationales only once the market is in ``REVEAL_STATES``, and shows
settlement as counts only (holdouts passed/total, finding titles and severities).
"""

from __future__ import annotations

import hmac
import io
import json
import logging
import os
import re
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import segno
from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, field_validator
from sqlalchemy import case, func, or_, select
from starlette.concurrency import run_in_threadpool
from starlette.datastructures import MutableHeaders
from starlette.routing import Mount
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from prm.config import RuntimeEnv, ServerConfig, load_server_config
from prm.db import init_db, make_engine, make_session_factory
from prm.github.webhook import Deduper, classify_pr_event, valid_repo_name, verify_signature
from prm.market import lmsr
from prm.market.engine import MarketEngine
from prm.models import Belief, Event, LeagueRun, Market, Position, Repo, Trade, Trader
from prm.types import REVEAL_STATES, State

log = logging.getLogger(__name__)
access_log = logging.getLogger("prm.access")

RunMarket = Callable[[str, int, str], Awaitable[object]]
STATIC_DIR = Path(__file__).resolve().parent / "dashboard" / "static"
CSP = (
    "default-src 'self'; script-src 'self' https://cdnjs.cloudflare.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; font-src https://fonts.gstatic.com; "
    "img-src 'self' data:; frame-ancestors 'none'"
)
SECURITY_HEADERS = {
    "Content-Security-Policy": CSP,
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "no-referrer",
}
_REQUEST_ID = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
PAGES = {"/": "index", "/league": "league", "/how": "how", "/judge": "judge", "/live": "live"}
_PLACEHOLDER = (
    "<!doctype html><html lang='en'><head><meta charset='utf-8'><title>PRediction Market</title></head>"
    "<body><h1>PRediction Market</h1><p>The {page} page is not built yet. The JSON API is under /api/.</p>"
    "</body></html>"
)


# ------------------------------------------------------------------------------------- auth declarations


def public() -> None:
    """Auth declaration: this route is deliberately public."""


def admin_token(request: Request) -> None:
    """``Authorization: Bearer <PRM_ADMIN_TOKEN>``, compared in constant time. Unset env -> 503."""
    expected = os.environ.get("PRM_ADMIN_TOKEN", "")
    if not expected:
        raise HTTPException(503, "admin token not configured")
    scheme, _, presented = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not hmac.compare_digest(presented.strip().encode(), expected.encode()):
        raise HTTPException(401, "unauthorized", headers={"WWW-Authenticate": "Bearer"})


async def github_hmac(request: Request) -> None:
    """GitHub's ``X-Hub-Signature-256`` over the raw body with ``GITHUB_WEBHOOK_SECRET``. Unset env -> 503."""
    key = os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    if not key:
        raise HTTPException(503, "webhook secret not configured")
    body = await request.body()
    if not verify_signature(key.encode(), body, request.headers.get("x-hub-signature-256")):
        raise HTTPException(401, "bad signature")


AUTH_DECLARATIONS = frozenset({public, admin_token, github_hmac})
PUBLIC, ADMIN, GITHUB_HMAC = Depends(public), Depends(admin_token), Depends(github_hmac)


def route_auth(route: APIRoute) -> list[Callable[..., Any]]:
    """The auth declarations attached to a route (a valid route has exactly one)."""
    return [d.call for d in route.dependant.dependencies if d.call in AUTH_DECLARATIONS]


def undeclared_routes(app: FastAPI) -> list[str]:
    """Routes without exactly one auth declaration. Only the ``/static`` mount is exempt (public assets)."""
    bad = []
    for route in app.routes:
        if isinstance(route, APIRoute):
            if len(route_auth(route)) != 1:
                bad.append(f"{sorted(route.methods)} {route.path}")
        elif not (isinstance(route, Mount) and route.path == "/static"):
            bad.append(getattr(route, "path", repr(route)))
    return bad


# -------------------------------------------------------------------------------------------- middleware


class RequestContextMiddleware:
    """Request id (honored if well-formed, else generated; echoed), one JSON access log line per request
    (method, path without query, status, ms, request id; never bodies) and security headers."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        incoming = dict(scope.get("headers") or []).get(b"x-request-id", b"").decode("latin-1")
        request_id = incoming if _REQUEST_ID.match(incoming) else uuid.uuid4().hex
        scope.setdefault("state", {})["request_id"] = request_id
        started, status, logged = time.perf_counter(), 500, False

        def write_log() -> None:
            ms = round((time.perf_counter() - started) * 1000, 1)
            line = {"method": scope["method"], "path": scope["path"], "status": status, "ms": ms}
            access_log.info(json.dumps({**line, "request_id": request_id}))

        async def wrapped_send(message: Message) -> None:
            nonlocal status, logged
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = MutableHeaders(scope=message)
                headers["X-Request-ID"] = request_id
                for name, value in SECURITY_HEADERS.items():
                    headers.setdefault(name, value)
            elif message["type"] == "http.response.body" and not message.get("more_body") and not logged:
                logged = True
                write_log()
            await send(message)

        try:
            await self.app(scope, receive, wrapped_send)
        finally:
            if not logged:
                write_log()


def _ensure_access_handler() -> None:
    if not access_log.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("%(message)s"))
        access_log.addHandler(handler)
        access_log.setLevel(logging.INFO)
        access_log.propagate = False


# ------------------------------------------------------------------------------------------- serializers


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    return (dt if dt.tzinfo else dt.replace(tzinfo=UTC)).isoformat()


def _price(m: Market) -> float:
    return m.final_price if m.final_price is not None else lmsr.price(m.q_yes, m.q_no, m.b)


_SUMMARY_FIELDS = (
    "id", "pr_number", "source", "state", "gate_state", "gate_description", "final_price", "outcome",
    "resolution", "escalations", "cost_usd", "latency_ms",
)  # fmt: skip


_BELIEF_FIELDS = (
    "escalation", "p_yes", "rationale", "model", "error", "input_tokens", "output_tokens", "latency_ms",
    "cost_usd",
)  # fmt: skip
_TRADE_FIELDS = (
    "id", "side", "shares", "cost", "price_before", "price_after", "escalation", "round", "created_at",
)  # fmt: skip
_EVENT_FIELDS = ("from_state", "to_state", "actor", "detail", "created_at")
_TRADER_FIELDS = ("name", "kind", "model", "bankroll")


def _pick(row: object, fields: tuple[str, ...]) -> dict[str, Any]:
    """An explicit whitelist of columns; datetimes become ISO strings in UTC."""
    out = {}
    for f in fields:
        value = getattr(row, f)
        out[f] = _iso(value) if isinstance(value, datetime) else value
    return out


def market_summary(m: Market, repo: str) -> dict[str, Any]:
    out = _pick(m, _SUMMARY_FIELDS)
    out.update(repo=repo, price=_price(m), created_at=_iso(m.created_at), settled_at=_iso(m.settled_at))
    return out


def _json_list(text: str | None) -> list[Any]:
    try:
        value = json.loads(text or "[]")
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def public_findings(findings_json: str | None) -> list[dict[str, str]]:
    """Red-team findings reduced to title + severity. Nothing else from settlement leaves the server."""
    out = []
    for f in _json_list(findings_json):
        if isinstance(f, dict):
            severity = f.get("severity") if f.get("severity") in ("high", "low") else "low"
            out.append({"title": str(f.get("title", ""))[:200], "severity": severity})
    return out


class RepoIn(BaseModel):
    model_config = ConfigDict(extra="forbid")
    full_name: str

    @field_validator("full_name")
    @classmethod
    def _owner_name(cls, v: str) -> str:
        v = v.strip()
        if not valid_repo_name(v):
            raise ValueError("full_name must look like 'owner/name'")
        return v


def _safe_qr_path(path: str) -> bool:
    return path.startswith("/") and not path.startswith("//") and "\\" not in path and path.isprintable()


class _NoBeliefs:
    """The API server never runs traders (the pipeline does); its engine only registers repos and reads."""

    async def get_belief(self, *_args: object, **_kwargs: object) -> object:
        raise RuntimeError("the API server does not run traders")


async def _default_run_market(repo: str, pr_number: int, head_sha: str) -> object:
    from prm.pipeline import run_pr_market  # lazy: the pipeline lands in its own WP E branch

    return await run_pr_market(repo, pr_number, head_sha)


# ------------------------------------------------------------------------------------------------- app


def create_app(
    env: RuntimeEnv | None = None,
    cfg: ServerConfig | None = None,
    *,
    run_market: RunMarket | None = None,
) -> FastAPI:
    env = RuntimeEnv.from_env() if env is None else env
    cfg = load_server_config(env.server_config) if cfg is None else cfg
    schedule = run_market or _default_run_market
    db_engine = make_engine(env.database_url)
    sf = make_session_factory(db_engine)
    engine = MarketEngine(sf, cfg, _NoBeliefs())
    deduper = Deduper()
    _ensure_access_handler()

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        init_db(db_engine)
        yield

    app = FastAPI(
        title="PRediction Market", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None
    )
    app.add_middleware(RequestContextMiddleware)
    app.state.session_factory, app.state.deduper, app.state.env = sf, deduper, env

    @app.get("/healthz", dependencies=[PUBLIC])
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # ------------------------------------------------------------------------------------------ webhook

    def registered_name(repo: str) -> str | None:
        with sf() as s:
            return s.scalar(select(Repo.full_name).where(func.lower(Repo.full_name) == repo.lower()))

    async def run_market_safely(repo: str, pr_number: int, head_sha: str) -> None:
        try:
            await schedule(repo, pr_number, head_sha)
        except Exception as exc:  # the pipeline fails closed itself; never take the server down with it
            log.error("run_market failed for %s#%s: %s", repo, pr_number, type(exc).__name__)
            deduper.forget(repo, pr_number, head_sha)  # a redelivery may retry

    @app.post("/api/hooks/github", status_code=202, dependencies=[GITHUB_HMAC])
    async def github_webhook(request: Request, background: BackgroundTasks) -> JSONResponse:
        event = request.headers.get("x-github-event", "")
        if event == "ping":
            return JSONResponse({"pong": True}, status_code=200)
        try:
            payload = json.loads(await request.body())
        except ValueError as exc:
            raise HTTPException(400, "webhook body must be JSON (content_type=json)") from exc
        result = classify_pr_event(event, payload)
        if isinstance(result, str):
            return JSONResponse({"ignored": result}, status_code=202)
        repo = await run_in_threadpool(registered_name, result.repo)
        if repo is None:
            return JSONResponse({"ignored": "repo not registered"}, status_code=202)
        if not deduper.first_time(repo, result.pr_number, result.head_sha):
            return JSONResponse({"ignored": "duplicate delivery"}, status_code=202)
        background.add_task(run_market_safely, repo, result.pr_number, result.head_sha)
        log.info("scheduled market for %s#%s at %s", repo, result.pr_number, result.head_sha[:12])
        scheduled = {"repo": repo, "pr_number": result.pr_number, "head_sha": result.head_sha}
        return JSONResponse({"scheduled": scheduled}, status_code=202)

    # -------------------------------------------------------------------------------------------- repos

    def repo_out(r: Repo) -> dict[str, Any]:
        return {"id": r.id, "full_name": r.full_name, "created_at": _iso(r.created_at)}

    @app.get("/api/repos", dependencies=[PUBLIC])
    def list_repos() -> dict[str, Any]:
        with sf() as s:
            return {"repos": [repo_out(r) for r in s.scalars(select(Repo).order_by(Repo.id))]}

    @app.post("/api/repos", status_code=201, dependencies=[ADMIN])
    def add_repo(body: RepoIn, response: Response) -> dict[str, Any]:
        existing = registered_name(body.full_name)
        if existing is not None:
            response.status_code = 200
        repo_id = engine.ensure_repo(existing or body.full_name)
        with sf() as s:
            return repo_out(s.get_one(Repo, repo_id))

    # ------------------------------------------------------------------------------------------ markets

    @app.get("/api/markets", dependencies=[PUBLIC])
    def list_markets(repo: str = "", limit: int = Query(50, ge=1, le=200)) -> dict[str, Any]:
        q = select(Market, Repo.full_name).join(Repo, Repo.id == Market.repo_id)
        if repo:
            q = q.where(func.lower(Repo.full_name) == repo.lower())
        with sf() as s:
            rows = s.execute(q.order_by(Market.id.desc()).limit(limit)).all()
            return {"markets": [market_summary(m, name) for m, name in rows]}

    @app.get("/api/markets/{market_id}", dependencies=[PUBLIC])
    def market_detail(market_id: int) -> dict[str, Any]:
        with sf() as s:
            q = (
                select(Market, Repo.full_name)
                .join(Repo, Repo.id == Market.repo_id)
                .where(Market.id == market_id)
            )
            row = s.execute(q).first()
            if row is None:
                raise HTTPException(404, "market not found")
            m, repo = row
            trades = s.execute(
                select(Trade, Trader.name, Trader.kind)
                .join(Trader, Trader.id == Trade.trader_id)
                .where(Trade.market_id == m.id)
                .order_by(Trade.id)
            ).all()
            events = s.scalars(select(Event).where(Event.market_id == m.id).order_by(Event.id)).all()
            revealed = State(m.state) in REVEAL_STATES
            beliefs = None
            if revealed:
                rows = s.execute(
                    select(Belief, Trader.name)
                    .join(Trader, Trader.id == Belief.trader_id)
                    .where(Belief.market_id == m.id)
                    .order_by(Belief.id)
                ).all()
                beliefs = [
                    {"trader": name, **_pick(b, _BELIEF_FIELDS), "key_risks": _json_list(b.key_risks_json)}
                    for b, name in rows
                ]
            out = market_summary(m, repo)
            out.update(
                head_sha=m.head_sha,
                base_sha=m.base_sha,
                b=m.b,
                initial_price=m.initial_price,
                closed_at=_iso(m.closed_at),
                gate={"state": m.gate_state, "description": m.gate_description},
                settlement={
                    "holdouts_passed": m.holdouts_passed,
                    "holdouts_total": m.holdouts_total,
                    "findings": public_findings(m.findings_json),
                },
                diff_text=m.diff_text,
                description_text=m.description_text,
                trades=[
                    {"trader": name, "kind": kind, **_pick(t, _TRADE_FIELDS)} for t, name, kind in trades
                ],
                events=[_pick(e, _EVENT_FIELDS) for e in events],
                beliefs_revealed=revealed,
                beliefs=beliefs,
            )
        out["status_description"] = engine.status_description(market_id)
        return out

    # ------------------------------------------------------------------------------------------ metrics

    @app.get("/api/metrics", dependencies=[PUBLIC])
    def metrics() -> dict[str, Any]:
        today = datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
        with sf() as s:
            by_state = {st.value: 0 for st in State}
            for state, n in s.execute(select(Market.state, func.count()).group_by(Market.state)):
                by_state[state] = n
            today_q = select(func.count(), func.coalesce(func.sum(Market.cost_usd), 0.0))
            markets_today, usd_today = s.execute(today_q.where(Market.created_at >= today)).one()
            mean_latency = s.scalar(select(func.avg(Market.latency_ms)).where(Market.latency_ms > 0))
            errors = s.scalar(
                select(func.count()).where(Market.state == State.VOID.value, Market.resolution == "void")
            )
            scored = s.execute(
                select(Market.final_price, Market.outcome).where(
                    Market.source == "github", Market.outcome.is_not(None), Market.final_price.is_not(None)
                )
            ).all()
            priced = s.scalar(
                select(func.count()).where(Market.source == "github", Market.final_price.is_not(None))
            )
        n = len(scored)
        brier = sum((p - (1.0 if o == "YES" else 0.0)) ** 2 for p, o in scored) / n if n else None
        return {
            "markets_by_state": by_state,
            "markets_today": markets_today,
            "llm_usd_today": round(float(usd_today), 6),
            "usd_per_market": round(float(usd_today) / markets_today, 6) if markets_today else None,
            "mean_latency_ms": round(float(mean_latency), 1) if mean_latency is not None else None,
            "errors": errors,
            "real_prs_priced": priced,
            "real_pr_brier": round(brier, 4) if brier is not None else None,
            "real_pr_n": n,
        }

    # ------------------------------------------------------------------------------- league, leaderboard

    @app.get("/api/league/report", dependencies=[PUBLIC])
    def league_report() -> Any:
        with sf() as s:
            text = s.scalar(
                select(LeagueRun.report_json)
                .where(LeagueRun.report_json.is_not(None))
                .order_by(LeagueRun.id.desc())
                .limit(1)
            )
        if text is None:
            raise HTTPException(404, "no league run yet")
        try:
            return json.loads(text)
        except ValueError as exc:
            raise HTTPException(500, "league report unreadable") from exc

    @app.get("/api/leaderboard", dependencies=[PUBLIC])
    def leaderboard(repo: str = "") -> dict[str, Any]:
        pnl = func.sum(case((Position.payout.is_not(None), Position.payout - Position.spent), else_=0.0))
        stats = (
            select(Position.trader_id, func.count().label("n"), pnl.label("pnl"))
            .group_by(Position.trader_id)
            .subquery()
        )
        q = (
            select(Trader, Repo.full_name, stats.c.n, stats.c.pnl)
            .outerjoin(Repo, Repo.id == Trader.repo_id)
            .outerjoin(stats, stats.c.trader_id == Trader.id)
        )
        if repo:
            q = q.where(or_(func.lower(Repo.full_name) == repo.lower(), Trader.repo_id.is_(None)))
        with sf() as s:
            rows = s.execute(q.order_by(Trader.bankroll.desc(), Trader.id)).all()
        traders = [
            {**_pick(t, _TRADER_FIELDS), "repo": name, "markets_traded": n or 0, "pnl": round(pl or 0.0, 6)}
            for t, name, n, pl in rows
        ]
        return {"traders": traders}

    # -------------------------------------------------------------------------------------------- QR

    @app.get("/api/qr.svg", dependencies=[PUBLIC])
    def qr_svg(path: str = Query("/judge", max_length=200)) -> Response:
        if not _safe_qr_path(path):
            raise HTTPException(422, "path must start with '/' and not with '//'")
        buf = io.BytesIO()
        segno.make(env.public_url + path, error="m").save(buf, kind="svg", scale=8, border=2, xmldecl=False)
        return Response(buf.getvalue(), media_type="image/svg+xml")

    # P1 (WP J) routes go here, each with its auth declaration: POST /api/demo/markets and
    # POST /api/demo/markets/{id}/close-and-settle (ADMIN), POST /api/judges (PUBLIC, rate-limited) and
    # POST /api/markets/{id}/human-trades (judge cookie). P1 remote settlement: POST /api/markets/{id}/settle
    # (HMAC with SETTLE_HMAC_SECRET). Add a new auth kind to AUTH_DECLARATIONS for the cookie and the HMAC.

    # ------------------------------------------------------------------------------------------ pages

    def page(name: str) -> Response:
        file = STATIC_DIR / f"{name}.html"
        if file.is_file():
            return FileResponse(file, media_type="text/html")
        return HTMLResponse(_PLACEHOLDER.format(page=name))

    def page_handler(name: str) -> Callable[[], Response]:
        def handler() -> Response:  # no parameters, so nothing from the request picks the file
            return page(name)

        return handler

    for path, name in PAGES.items():
        app.add_api_route(path, page_handler(name), methods=["GET"], dependencies=[PUBLIC])

    @app.get("/m/{market_id}", dependencies=[PUBLIC], include_in_schema=False)
    def market_page(market_id: int) -> Response:
        return page("market")

    app.mount("/static", StaticFiles(directory=STATIC_DIR, check_dir=False), name="static")

    missing = undeclared_routes(app)
    if missing:
        raise RuntimeError(f"routes without an auth declaration: {missing}")
    return app
