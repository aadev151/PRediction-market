"""WP E. run_pr_market(repo, pr, sha); any exception -> void + failure status. PLAN §6.13.

GitHub: status pending -> ``.prm.toml`` from the BASE sha -> PR, diff and both trees -> engine create /
run_agents (escalations post pending) / close / gate -> status. The market then stays GATED: post-merge
settlement (DECISIONS #57) needs the merge event and a clone, which aren't wired yet.

Local demo (``run_local_market``): the same engine path on a local target plus a patch, no GitHub calls. It
then settles with the REAL post-merge oracle in a throwaway git repo: the patch is merged with ``--no-ff``,
and either reverted (``after_merge="revert"`` -> NO) or left alone with the window fast-forwarded
(``"quiet"`` -> YES).

Fail closed: any exception VOIDs the market (if one exists) and posts ``failure``. Only the exception type is
logged or stored (void reasons are public on the API).
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
from datetime import datetime, timedelta
from pathlib import Path
from typing import Literal

from sqlalchemy.orm import Session, sessionmaker

from prm.config import (
    TARGET_CONFIG_FILENAME,
    ConfigError,
    RuntimeEnv,
    ServerConfig,
    load_server_config,
    load_target_config,
    parse_target_config,
)
from prm.db import make_session_factory
from prm.github.client import GitHubClient, check_repo
from prm.market.engine import MarketEngine
from prm.models import Market, utcnow
from prm.settle.postmerge import MergedPR, evaluate, settle_postmerge
from prm.types import PRBundle, SettlementResult, State

log = logging.getLogger(__name__)

# (state, description, target_url) -> posts a `pr-market` status (GitHub) or records it (local demo).
StatusSink = Callable[[str, str, str | None], Awaitable[None]]
AfterMerge = Literal["revert", "quiet"]

MAX_CONCURRENT_MARKETS = 2
OPEN_DESCRIPTION = "market open…"
NO_CONFIG_DESCRIPTION = "no .prm.toml on base branch"
BAD_CONFIG_DESCRIPTION = "invalid .prm.toml on base branch"
SUPERSEDED_DESCRIPTION = "superseded: the PR head moved"
FACTORY_ERROR = "factory error: human review required"
COPY_IGNORE = shutil.ignore_patterns(".venv", "__pycache__", ".git", ".pytest_cache", ".prm-cache")
DEMO_BRANCH = "main"
REVERT_DELAY = timedelta(hours=1)  # demo: the revert lands an hour after the merge...
REVERT_SETTLE_AFTER = timedelta(days=1)  # ...and the oracle looks a day after the merge
_DONE = frozenset({State.GATED, State.SETTLING, State.SETTLED, State.VOID})


class PatchError(RuntimeError):
    """``git apply`` could not apply the demo patch."""


class DemoGitError(RuntimeError):
    """A git step building the throwaway demo repo failed."""


@dataclass
class PipelineDeps:
    """Everything the pipeline touches, injectable for tests. ``from_env`` builds the production defaults."""

    env: RuntimeEnv
    cfg: ServerConfig
    session_factory: sessionmaker[Session]
    engine: MarketEngine
    github: GitHubClient
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
        clock: Callable[[], datetime] = utcnow,
    ) -> PipelineDeps:
        from prm.traders.roster import make_engine_provider

        env = env or RuntimeEnv.from_env()
        cfg = cfg or load_server_config(env.server_config)
        sf = session_factory or make_session_factory(env)
        llm = provider or make_engine_provider(cfg, env)
        engine = MarketEngine(sf, cfg, llm, clock=clock)  # type: ignore[arg-type]
        return cls(env, cfg, sf, engine, github or GitHubClient(), clock)


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


def _state(deps: PipelineDeps, market_id: int) -> State:
    with deps.session_factory() as s:
        m = s.get(Market, market_id)
        return State(m.state) if m is not None else State.VOID


def _work_dir(deps: PipelineDeps, prefix: str) -> Path:
    cache = Path(deps.env.cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=cache)).resolve()


async def _post_final(
    deps: PipelineDeps, market_id: int, bundle: PRBundle, sink: StatusSink, url: str
) -> None:
    decision = deps.engine.gate(market_id, bundle.target.gate)  # idempotent; VOID -> failure
    await sink(decision.state, deps.engine.status_description(market_id), url)


async def _open_and_gate(deps: PipelineDeps, bundle: PRBundle, sink: StatusSink, holder: list[int]) -> bool:
    """create -> run_agents -> close -> gate -> status. ``holder`` gets the market id as soon as it exists,
    so the caller can void it on any exception. True if the market is now freshly GATED."""
    engine = deps.engine
    market_id = engine.create(engine.ensure_repo(bundle.repo), bundle)
    holder.append(market_id)
    url = f"{deps.env.public_url}/m/{market_id}"
    state = _state(deps, market_id)
    if state != State.CONTEXT_READY:  # an existing market for this sha (a redelivery or a rerun)
        if state in _DONE:
            await _post_final(deps, market_id, bundle, sink, url)
        return False

    async def on_escalation(k: int, max_rounds: int, p: float) -> None:
        await sink("pending", f"escalation {k}/{max_rounds} (p={p:.2f})", url)

    await engine.run_agents(market_id, bundle, on_escalation=on_escalation)
    if _state(deps, market_id) == State.VOID:  # too few traders responded
        await _post_final(deps, market_id, bundle, sink, url)
        return False
    engine.close(market_id)
    decision = engine.gate(market_id, bundle.target.gate)
    await sink(decision.state, decision.description, url)
    return _state(deps, market_id) == State.GATED


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
    """Run one GitHub PR market up to its gate and post the status. Returns the market id (None if no market
    was opened). Never raises (except cancellation, after voiding): every error is a failure status."""
    try:
        deps = deps or default_deps()
    except Exception as exc:
        log.error("pipeline: cannot build dependencies (%s)", type(exc).__name__)
        return None
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
            # GATED is final for now: post-merge settlement needs the merge event and a clone (not wired).
            await _open_and_gate(deps, bundle, post, holder)
            return holder[0] if holder else None
        except asyncio.CancelledError as exc:
            await _fail_closed(deps, exc, holder, None)
            raise
        except Exception as exc:
            await _fail_closed(deps, exc, holder, post)
            return holder[0] if holder else None
        finally:
            if work is not None:
                shutil.rmtree(work, ignore_errors=True)


# ------------------------------------------------------------------------------------------- local demo


def _git_env(work: Path, when: datetime | None = None) -> dict[str, str]:
    """No factory secrets, no user/system git config, git fenced to ``work`` (an enclosing checkout can't
    swallow the paths), and optional fixed commit dates so the oracle sees a deterministic history."""
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(work),
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CEILING_DIRECTORIES": str(work),
    }
    if when is not None:
        stamp = f"@{int(when.timestamp())} +0000"
        env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = stamp
    return env


def _git(repo: Path, work: Path, *args: str, when: datetime | None = None) -> str:
    git = shutil.which("git")
    if git is None:
        raise DemoGitError("git not found on PATH")
    argv = [
        git, "-C", str(repo), "-c", "user.name=prm", "-c", "user.email=prm@localhost",
        "-c", "commit.gpgsign=false", *args,
    ]  # fmt: skip
    proc = subprocess.run(  # noqa: S603 - fixed argv, shell=False
        argv, env=_git_env(work, when), shell=False, capture_output=True, text=True, timeout=60, check=False
    )
    if proc.returncode != 0:
        raise DemoGitError(f"git {args[0]} exited {proc.returncode}")
    return proc.stdout.strip()


def apply_patch(tree: Path, patch: Path) -> None:
    """``git apply`` a target-relative patch inside ``tree`` (shell=False), fenced to ``tree``'s parent."""
    tree = Path(tree).resolve()
    try:
        _git(tree, tree.parent, "apply", "--whitespace=nowarn", str(Path(patch).resolve()))
    except DemoGitError as exc:
        raise PatchError(f"git apply {Path(patch).name} failed ({exc})") from None


def _demo_repo(root: Path, patch: Path, work: Path, message: str) -> tuple[Path, str]:
    """A throwaway repo: commit the target as "base" on ``main``, the patched tree on branch ``pr``.
    Returns (repo, pr_commit)."""
    repo = work / "repo"
    shutil.copytree(root, repo, ignore=COPY_IGNORE)
    _git(repo, work, "init", "-q")
    _git(repo, work, "symbolic-ref", "HEAD", f"refs/heads/{DEMO_BRANCH}")
    _git(repo, work, "add", "-A")
    _git(repo, work, "commit", "-q", "-m", "base")
    _git(repo, work, "checkout", "-q", "-b", "pr")
    apply_patch(repo, patch)
    _git(repo, work, "add", "-A")
    _git(repo, work, "commit", "-q", "-m", message)
    return repo, _git(repo, work, "rev-parse", "HEAD")


def _merge_and_settle(
    repo: Path,
    work: Path,
    pr_commit: str,
    after_merge: AfterMerge,
    merged_at: datetime,
    window_days: int,
) -> SettlementResult:
    """Merge ``pr`` with --no-ff at ``merged_at``, optionally revert it, then ask the post-merge oracle
    with the target's ``[postmerge].window_days`` (DECISIONS #58)."""
    _git(repo, work, "checkout", "-q", DEMO_BRANCH)
    _git(repo, work, "merge", "-q", "--no-ff", "-m", "Merge demo PR", "pr", when=merged_at)
    merged = MergedPR(_git(repo, work, "rev-parse", "HEAD"), merged_at)
    if after_merge == "revert":
        _git(repo, work, "revert", "--no-edit", pr_commit, when=merged_at + REVERT_DELAY)
        now = merged_at + REVERT_SETTLE_AFTER
    else:  # fast-forward the window: a quiet PR settles YES at its end
        now = evaluate(repo, merged, now=merged_at, branch=DEMO_BRANCH, window_days=window_days).settles_at
    result = settle_postmerge(repo, merged, now=now, branch=DEMO_BRANCH, window_days=window_days)
    return result or SettlementResult.infra_error("post-merge settlement still pending")


async def run_local_market(
    target_dir: Path,
    patch: Path,
    *,
    repo: str,
    description: str = "local demo market",
    after_merge: AfterMerge = "quiet",
    deps: PipelineDeps | None = None,
    statuses: list[tuple[str, str, str | None]] | None = None,
) -> int:
    """A full demo market on a local target + patch: no GitHub calls, written to the DB for the dashboard,
    settled by the real post-merge oracle (``after_merge``: "revert" -> NO, "quiet" -> YES).

    ``statuses`` (optional) collects the status sequence a PR would have seen. Raises ``ConfigError`` /
    ``PatchError`` before a market exists; after that every error VOIDs the market and its id is returned.
    """
    deps = deps or default_deps()
    check_repo(repo)
    if after_merge not in ("revert", "quiet"):
        raise ValueError(f"after_merge must be 'revert' or 'quiet', got {after_merge!r}")
    root = Path(target_dir).resolve()
    target = load_target_config(root)
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
            git_repo, pr_commit = await asyncio.to_thread(_demo_repo, root, patch, work, description)
            bundle = PRBundle("demo", repo, None, None, None, diff, description, base_dir, head_dir, target)
            await record("pending", OPEN_DESCRIPTION, None)
            try:
                if await _open_and_gate(deps, bundle, record, holder):
                    market_id = holder[0]
                    merged_at = deps.clock()
                    result = await asyncio.to_thread(
                        _merge_and_settle,
                        git_repo,
                        work,
                        pr_commit,
                        after_merge,
                        merged_at,
                        target.postmerge.window_days,
                    )
                    deps.engine.settle(market_id, result)
                    await _post_final(deps, market_id, bundle, record, f"{deps.env.public_url}/m/{market_id}")
            except Exception as exc:
                await _fail_closed(deps, exc, holder, record)
                if not holder:
                    raise
            return holder[0]
        finally:
            shutil.rmtree(work, ignore_errors=True)


@dataclass(frozen=True)
class MarketSummary:
    """What the CLI prints after a market."""

    id: int
    state: str
    final_price: float | None
    gate: str | None
    outcome: str | None
    description: str


def summarize(deps: PipelineDeps, market_id: int) -> MarketSummary:
    with deps.session_factory() as s:
        m = s.get(Market, market_id)
        if m is None:
            raise LookupError(market_id)
        row = (m.state, m.final_price, m.gate_state, m.outcome)
    return MarketSummary(market_id, *row, deps.engine.status_description(market_id))
