"""WP E. run_pr_market(repo, pr, sha); any exception -> void + failure status. PLAN §6.13.

One market per PR head: status pending -> ``.prm.toml`` from the BASE sha -> PR, diff and both trees -> engine
create / run_agents (escalations post pending) / close / gate -> status -> settle -> final status.
``run_local_market`` runs the same engine path on a local target plus a patch, with no GitHub calls.

Fail closed: any exception VOIDs the market (if one exists) and posts ``failure``; only the exception type is
logged. The settler is injectable; the default is WP D's ``settle_tree``, and if that isn't importable the
result is an infra error, which VOIDs the market.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import tempfile
import weakref
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from sqlalchemy.orm import Session, sessionmaker

from prm.config import (
    TARGET_CONFIG_FILENAME,
    ConfigError,
    RuntimeEnv,
    ServerConfig,
    TargetConfig,
    load_server_config,
    load_target_config,
    parse_target_config,
)
from prm.db import make_session_factory
from prm.github.client import GitHubClient, check_repo
from prm.market.engine import MarketEngine
from prm.models import Market, utcnow
from prm.types import PRBundle, SettlementResult, State

log = logging.getLogger(__name__)

Settler = Callable[[Path, TargetConfig], SettlementResult]
# (state, description, target_url) -> posts a `pr-market` status (GitHub) or records it (local demo).
StatusSink = Callable[[str, str, str | None], Awaitable[None]]

MAX_CONCURRENT_MARKETS = 2
OPEN_DESCRIPTION = "market open…"
NO_CONFIG_DESCRIPTION = "no .prm.toml on base branch"
BAD_CONFIG_DESCRIPTION = "invalid .prm.toml on base branch"
SUPERSEDED_DESCRIPTION = "superseded: the PR head moved"
FACTORY_ERROR = "factory error: human review required"
COPY_IGNORE = shutil.ignore_patterns(".venv", "__pycache__", ".git", ".pytest_cache", ".prm-cache")
_DONE = frozenset({State.GATED, State.SETTLING, State.SETTLED, State.VOID})


class PatchError(RuntimeError):
    """``git apply`` could not apply the demo patch."""


def default_settler(head_dir: Path, target: TargetConfig) -> SettlementResult:
    """WP D's local settler, imported lazily. Not available yet -> infra error, which VOIDs the market."""
    try:
        from prm.settle.local import settle_tree  # type: ignore[attr-defined]
    except ImportError:
        return SettlementResult.infra_error("settlement not available")
    return settle_tree(head_dir, target)


@dataclass
class PipelineDeps:
    """Everything the pipeline touches, injectable for tests. ``from_env`` builds the production defaults."""

    env: RuntimeEnv
    cfg: ServerConfig
    session_factory: sessionmaker[Session]
    engine: MarketEngine
    github: GitHubClient
    settler: Settler = default_settler
    clock: Callable[[], datetime] = utcnow

    @classmethod
    def from_env(
        cls,
        env: RuntimeEnv | None = None,
        *,
        cfg: ServerConfig | None = None,
        session_factory: sessionmaker[Session] | None = None,
        provider: object | None = None,
        github: GitHubClient | None = None,
        settler: Settler | None = None,
        clock: Callable[[], datetime] = utcnow,
    ) -> PipelineDeps:
        from prm.traders.roster import make_engine_provider

        env = env or RuntimeEnv.from_env()
        cfg = cfg or load_server_config(env.server_config)
        sf = session_factory or make_session_factory(env)
        llm = provider or make_engine_provider(cfg, env)
        engine = MarketEngine(sf, cfg, llm, clock=clock)  # type: ignore[arg-type]
        return cls(env, cfg, sf, engine, github or GitHubClient(), settler or default_settler, clock)


_default: PipelineDeps | None = None


def configure(deps: PipelineDeps | None) -> None:
    """Install the deps ``run_pr_market(repo, pr, sha)`` uses when none are passed (e.g. the server's)."""
    global _default
    _default = deps


def default_deps() -> PipelineDeps:
    global _default
    if _default is None:
        _default = PipelineDeps.from_env()
    return _default


_slots: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Semaphore] = weakref.WeakKeyDictionary()


def _market_slot() -> asyncio.Semaphore:
    """The global Semaphore(2) around markets (one per event loop, so tests with fresh loops stay safe)."""
    loop = asyncio.get_running_loop()
    sem = _slots.get(loop)
    if sem is None:
        sem = _slots[loop] = asyncio.Semaphore(MAX_CONCURRENT_MARKETS)
    return sem


def absolute_holdouts(target: TargetConfig, target_root: Path) -> TargetConfig:
    """Resolve a local ``holdouts_path`` (relative to the target root) to an absolute path, so the settler
    can run on a temp copy of the tree. Targets with ``holdouts_repo`` are returned unchanged."""
    path = target.holdouts_dir(target_root)
    if path is None:
        return target
    section = target.target.model_copy(update={"holdouts_path": str(path)})
    return target.model_copy(update={"target": section})


def _state(deps: PipelineDeps, market_id: int) -> State:
    with deps.session_factory() as s:
        m = s.get(Market, market_id)
        return State(m.state) if m is not None else State.VOID


def _work_dir(deps: PipelineDeps, prefix: str) -> Path:
    cache = Path(deps.env.cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=cache))


async def _post_final(
    deps: PipelineDeps, market_id: int, target: TargetConfig, sink: StatusSink, url: str
) -> None:
    decision = deps.engine.gate(market_id, target.gate)  # idempotent; VOID -> failure
    await sink(decision.state, deps.engine.status_description(market_id), url)


async def _run_market(deps: PipelineDeps, bundle: PRBundle, sink: StatusSink, holder: list[int]) -> int:
    """create -> run_agents -> close -> gate -> status -> settle -> final status. ``holder`` gets the market
    id as soon as it exists, so the caller can void it on any exception."""
    engine, target = deps.engine, bundle.target
    repo_id = engine.ensure_repo(bundle.repo)
    market_id = engine.create(repo_id, bundle)
    holder.append(market_id)
    url = f"{deps.env.public_url}/m/{market_id}"
    state = _state(deps, market_id)
    if state != State.CONTEXT_READY:  # an existing market for this sha (a redelivery or a rerun)
        if state in _DONE:
            await _post_final(deps, market_id, target, sink, url)
        return market_id

    async def on_escalation(k: int, max_rounds: int, p: float) -> None:
        await sink("pending", f"escalation {k}/{max_rounds} (p={p:.2f})", url)

    await engine.run_agents(market_id, bundle, on_escalation=on_escalation)
    if _state(deps, market_id) == State.VOID:  # too few traders responded
        await _post_final(deps, market_id, target, sink, url)
        return market_id
    engine.close(market_id)
    decision = engine.gate(market_id, target.gate)
    await sink(decision.state, decision.description, url)
    if _state(deps, market_id) != State.GATED:
        return market_id
    if target.settle.driver == "remote":  # P1: the target's action POSTs the counts later
        return market_id
    result = await asyncio.to_thread(deps.settler, bundle.head_dir, target)
    engine.settle(market_id, result)
    await _post_final(deps, market_id, target, sink, url)
    return market_id


async def _fail_closed(
    deps: PipelineDeps, exc: BaseException, holder: list[int], sink: StatusSink | None
) -> None:
    log.error("pipeline: factory error (%s)", type(exc).__name__)
    market_id = holder[0] if holder else None
    if market_id is not None:
        try:
            deps.engine.void(market_id, f"factory error: {type(exc).__name__}")
        except Exception as void_exc:
            log.error("pipeline: void after factory error failed (%s)", type(void_exc).__name__)
    if sink is None:
        return
    url = f"{deps.env.public_url}/m/{market_id}" if market_id is not None else None
    try:
        await sink("failure", FACTORY_ERROR, url)
    except Exception as post_exc:
        log.error("pipeline: could not post the failure status (%s)", type(post_exc).__name__)


async def run_pr_market(repo: str, pr: int, sha: str, *, deps: PipelineDeps | None = None) -> int | None:
    """Run one GitHub PR market end to end. Returns the market id (None if no market was opened)."""
    deps = deps or default_deps()
    gh = deps.github

    async def post(state: str, description: str, url: str | None) -> None:
        await gh.post_status(repo, sha, state, description, url)  # type: ignore[arg-type]

    holder: list[int] = []
    async with _market_slot():
        work: Path | None = None
        try:
            check_repo(repo)
            await post("pending", OPEN_DESCRIPTION, None)
            info = await gh.get_pr(repo, pr)
            if info.head_sha != sha:
                await post("error", SUPERSEDED_DESCRIPTION, None)
                return None
            text = await gh.get_file(repo, TARGET_CONFIG_FILENAME, info.base_sha)
            if text is None:
                await post("error", NO_CONFIG_DESCRIPTION, None)
                return None
            try:
                target = parse_target_config(text)
            except ConfigError:
                await post("failure", BAD_CONFIG_DESCRIPTION, None)
                return None
            diff = await gh.get_diff(repo, pr)
            work = _work_dir(deps, "prm-pr-")
            base_dir = await gh.download_tree(repo, info.base_sha, work / "base")
            head_dir = await gh.download_tree(repo, sha, work / "head")
            description = f"{info.title}\n\n{info.body}".strip()
            bundle = PRBundle(
                "github", repo, pr, sha, info.base_sha, diff, description, base_dir, head_dir, target
            )
            return await _run_market(deps, bundle, post, holder)
        except asyncio.CancelledError as exc:
            await _fail_closed(deps, exc, holder, None)
            raise
        except Exception as exc:
            await _fail_closed(deps, exc, holder, post)
            return holder[0] if holder else None
        finally:
            if work is not None:
                shutil.rmtree(work, ignore_errors=True)


def apply_patch(tree: Path, patch: Path) -> None:
    """``git apply`` a target-relative patch inside ``tree`` (shell=False). Git is fenced to ``tree``, so an
    enclosing repository (e.g. the cache dir inside a checkout) can't swallow the patch paths."""
    git = shutil.which("git")
    if git is None:
        raise PatchError("git not found on PATH")
    env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(Path(tree).resolve().parent)}
    proc = subprocess.run(  # noqa: S603 - fixed argv, shell=False
        [git, "apply", "--whitespace=nowarn", str(Path(patch).resolve())],
        cwd=tree, env=env, shell=False, capture_output=True, text=True, timeout=60, check=False,
    )  # fmt: skip
    if proc.returncode != 0:
        raise PatchError(f"git apply {Path(patch).name} failed: {proc.stderr.strip()[:300]}")


async def run_local_market(
    target_dir: Path,
    patch: Path,
    *,
    repo: str,
    description: str = "local demo market",
    deps: PipelineDeps | None = None,
    statuses: list[tuple[str, str, str | None]] | None = None,
) -> int:
    """A full demo market on a local target + patch: no GitHub calls, written to the DB for the dashboard.

    ``statuses`` (optional) collects the status sequence a PR would have seen. Raises ``ConfigError`` /
    ``PatchError`` before a market exists; after that every error VOIDs the market and its id is returned.
    """
    deps = deps or default_deps()
    check_repo(repo)
    root = Path(target_dir).resolve()
    target = absolute_holdouts(load_target_config(root), root)
    patch = Path(patch).resolve()
    diff = patch.read_text(encoding="utf-8")
    seen = statuses if statuses is not None else []

    async def record(state: str, desc: str, url: str | None) -> None:
        seen.append((state, desc, url))

    holder: list[int] = []
    async with _market_slot():
        work = _work_dir(deps, "prm-demo-")
        try:
            base_dir, head_dir = work / "base", work / "head"
            shutil.copytree(root, base_dir, ignore=COPY_IGNORE)
            shutil.copytree(root, head_dir, ignore=COPY_IGNORE)
            apply_patch(head_dir, patch)
            bundle = PRBundle("demo", repo, None, None, None, diff, description, base_dir, head_dir, target)
            await record("pending", OPEN_DESCRIPTION, None)
            try:
                return await _run_market(deps, bundle, record, holder)
            except Exception as exc:
                await _fail_closed(deps, exc, holder, record)
                if not holder:
                    raise
                return holder[0]
        finally:
            shutil.rmtree(work, ignore_errors=True)


@dataclass(frozen=True)
class MarketSummary:
    """What the CLI prints after a market (counts only, never holdout names)."""

    id: int
    state: str
    final_price: float | None
    gate: str | None
    outcome: str | None
    holdouts_passed: int | None
    holdouts_total: int | None
    description: str


def summarize(deps: PipelineDeps, market_id: int) -> MarketSummary:
    with deps.session_factory() as s:
        m = s.get(Market, market_id)
        if m is None:
            raise LookupError(market_id)
        row = (m.state, m.final_price, m.gate_state, m.outcome, m.holdouts_passed, m.holdouts_total)
    return MarketSummary(market_id, *row, deps.engine.status_description(market_id))
