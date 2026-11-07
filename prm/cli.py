"""`prm` command line (PLAN §6.15).

Phase 0 ships `prm --version` and `prm validate`; WP B adds `prm smoke-llm`.
WP E adds serve, repo add, market, settle and demo (WP G adds league).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from prm import __version__
from prm.config import ConfigError, RuntimeEnv, load_server_config, load_target_config

if TYPE_CHECKING:
    from prm.pipeline import MarketSummary


def _cmd_validate(args: argparse.Namespace) -> int:
    try:
        server = load_server_config(args.server)
        gate = server.gate
        print(f"server config OK: {len(server.traders)} traders, gate {gate.human_review}/{gate.auto_merge}")
        if args.target:
            target = load_target_config(args.target)
            gate = server.gate_for(target.gate)
            print(
                f"target config OK: {target.target.name} · driver={target.settle.driver} · "
                f"gate {gate.human_review}/{gate.auto_merge}"
            )
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    return 0


def _cmd_smoke_llm(args: argparse.Namespace) -> int:
    from prm.traders.llm import smoke

    if not os.environ.get("ANTHROPIC_API_KEY"):
        print("ANTHROPIC_API_KEY is not set", file=sys.stderr)
        return 2
    try:
        cfg = load_server_config(args.server)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    results = asyncio.run(smoke(cfg))
    for r in results:
        if r.abstained:
            print(f"{r.model}: ABSTAINED ({r.error})")
        else:
            print(
                f"{r.model}: p_yes={r.p_yes:.2f} cost=${r.cost_usd:.6f} latency={r.latency_ms}ms "
                f"tokens={r.input_tokens}/{r.output_tokens} rationale={r.rationale[:80]!r}"
            )
    return 1 if any(r.abstained for r in results) else 0


_REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_PR_REF = re.compile(r"^([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)#([0-9]+)$")
DEMO_PATCHES = {"bug": "bug_auth.patch", "good": "good_docstring.patch"}
# What happens after the demo merge: the bug gets reverted (settles NO), the good change stays (YES).
DEMO_AFTER_MERGE: dict[str, Literal["revert", "quiet"]] = {"bug": "revert", "good": "quiet"}


def _repo_name(value: str) -> str:
    if not _REPO.match(value) or any(p in {".", ".."} for p in value.split("/")):
        raise argparse.ArgumentTypeError(f"expected owner/name, got {value!r}")
    return value


def _pr_ref(value: str) -> tuple[str, int]:
    m = _PR_REF.match(value)
    if m is None:
        raise argparse.ArgumentTypeError(f"expected owner/repo#N, got {value!r}")
    return _repo_name(m.group(1)), int(m.group(2))


def _cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("prm.server:create_app", factory=True, host=args.host, port=args.port, reload=args.reload)
    return 0


def _cmd_repo_add(args: argparse.Namespace) -> int:
    from prm import pipeline

    repo_id = pipeline.default_deps().engine.ensure_repo(args.name)
    print(f"repo {args.name} registered (id {repo_id})")
    return 0


def _print_summary(summary: MarketSummary, public_url: str) -> None:
    price = "n/a" if summary.final_price is None else f"{summary.final_price:.2f}"
    print(f"market {summary.id}: {summary.state}")
    print(f"price {price} · gate {summary.gate or 'none'} · outcome {summary.outcome or 'none'}")
    print(f"status: {summary.description}")
    print(f"dashboard: {public_url}/m/{summary.id}")


def _cmd_market(args: argparse.Namespace) -> int:
    from prm import pipeline

    repo, number = args.pr
    deps = pipeline.default_deps()

    async def run() -> int | None:
        info = await deps.github.get_pr(repo, number)
        return await pipeline.run_pr_market(repo, number, info.head_sha, deps=deps)

    try:
        market_id = asyncio.run(run())
    except Exception as exc:  # only the type: an error message could carry request data
        print(f"market failed: {type(exc).__name__}", file=sys.stderr)
        return 1
    if market_id is None:
        print("no market opened (see the pr-market status on the PR)", file=sys.stderr)
        return 1
    _print_summary(pipeline.summarize(deps, market_id), deps.env.public_url)
    return 0


SETTLE_MOVED = (
    "settlement is post-merge now (DECISIONS #57): a merged PR settles NO on a revert or fix to its lines "
    "within 14 days, YES otherwise. There is no tree to settle; try `prm demo bug|good --local`."
)


def _cmd_settle(args: argparse.Namespace) -> int:
    print(SETTLE_MOVED, file=sys.stderr)
    return 2


def _cmd_demo(args: argparse.Namespace) -> int:
    if args.action == "reset":
        return _demo_reset(args)
    if not args.local:
        print("demo without --local (open a real PR) is not implemented yet; use --local", file=sys.stderr)
        return 2
    if not args.target or not args.repo:
        print("demo --local needs --target PATH and --repo owner/name", file=sys.stderr)
        return 2
    from prm import pipeline

    target = Path(args.target).resolve()
    patch = Path(args.patch) if args.patch else target.parent / "patches" / DEMO_PATCHES[args.action]
    if not patch.is_file():
        print(f"patch not found: {patch} (pass --patch)", file=sys.stderr)
        return 2
    deps = pipeline.default_deps()
    run = pipeline.run_local_market(
        target, patch, repo=args.repo, description=f"demo: {patch.name}",
        after_merge=DEMO_AFTER_MERGE[args.action], deps=deps,
    )  # fmt: skip
    try:
        market_id = asyncio.run(run)
    except (ConfigError, pipeline.PatchError, pipeline.DemoGitError) as exc:
        print(f"demo failed: {exc}", file=sys.stderr)
        return 1
    summary = pipeline.summarize(deps, market_id)
    print(f"demo {args.action}: {patch.name} on {args.repo}")
    _print_summary(summary, deps.env.public_url)
    return 0 if summary.state == "SETTLED" else 1


def _demo_reset(args: argparse.Namespace) -> int:
    env = RuntimeEnv.from_env()
    if env.database_url_override:
        print("DATABASE_URL is set: demo reset only deletes a local SQLite file", file=sys.stderr)
        return 2
    db = Path(env.db_path)
    if not args.yes:
        print(f"refusing to delete {db} without --yes", file=sys.stderr)
        return 2
    removed = [p for p in (db, Path(f"{db}-wal"), Path(f"{db}-shm"), Path(f"{db}-journal")) if p.is_file()]
    for p in removed:
        p.unlink()
    print(f"deleted {db}" if removed else f"nothing to delete ({db} does not exist)")
    return 0


def _cmd_league(args: argparse.Namespace) -> int:
    """Mutants -> parallel cached beliefs + hidden-test oracle -> sequential replay -> aggregates (WP G)."""
    from prm.db import make_session_factory
    from prm.league.report import publish
    from prm.league.run import default_out_dir, run_league

    try:
        from prm.league.mutate import generate_mutants  # type: ignore[attr-defined]
    except ImportError:
        print("league: mutant generation (prm.league.mutate) is not available yet", file=sys.stderr)
        return 2
    try:
        cfg = load_server_config(args.server)
        target_dir = Path(args.target).resolve()
        target = load_target_config(target_dir)
    except ConfigError as exc:
        print(f"league: {exc}", file=sys.stderr)
        return 1
    lp = cfg.league
    n = args.n if args.n is not None else lp.n
    seed = args.seed if args.seed is not None else lp.seed
    bug_fraction = args.bug_fraction if args.bug_fraction is not None else lp.bug_fraction
    concurrency = args.concurrency if args.concurrency is not None else lp.concurrency
    cfg = cfg.model_copy(update={"league": lp.model_copy(update={"n": n, "seed": seed})})
    out = Path(args.out) if args.out else default_out_dir(target.target.name)
    mutants = generate_mutants(
        target_dir, out / f"mutants-seed{seed}-n{n}-bug{bug_fraction}", n=n, seed=seed,
        bug_fraction=bug_fraction, ctx=target.context,
    )  # fmt: skip
    params = {"bug_fraction": bug_fraction}
    report = asyncio.run(
        run_league(target_dir, mutants, out_dir=out, cfg=cfg, concurrency=concurrency, params=params)
    )
    publish(
        report, docs_path=Path(args.docs) if args.docs else None,
        session_factory=make_session_factory(RuntimeEnv.from_env()), repo_label=report["params"]["repo"],
    )  # fmt: skip
    _print_league_headline(report)
    return 0


def _print_league_headline(report: dict) -> None:
    def line(label: str, s: dict) -> str:
        return f"  {label:<26} Brier {s['brier']} (n={s['n']})"

    scores = report["scores"]["all"]
    print(f"league: {report['n_markets']} markets, {report['n_scored']} scored, beliefs {report['beliefs']}")
    print(line("market", scores["market"]))
    print(line("simple average", scores["average"]))
    best = scores["best_single"]
    print(line(f"best single ({best.get('trader')})", best) + " [chosen in hindsight]")
    row = next(r for r in report["gate_economics"]["thresholds"] if r["threshold"] == 0.90)
    print(
        f"  gate 0.90: auto {row['auto_pct']}% (n={row['n_auto']}), escaped defects "
        f"{row['escaped_defect_rate']}%, false rejects {row['false_reject_rate']}% (n={row['n_rejected']})"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="prm", description="PRediction Market factory")
    parser.add_argument("--version", action="version", version=f"prm {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate", help="validate prm.server.toml and optionally a target's .prm.toml")
    validate.add_argument("--server", default=None, help="server config path (default: $PRM_SERVER_CONFIG)")
    validate.add_argument("--target", default=None, help="target root or .prm.toml path")
    validate.set_defaults(func=_cmd_validate)

    smoke = sub.add_parser(
        "smoke-llm", help="one real LLM call per configured model (needs ANTHROPIC_API_KEY)"
    )
    smoke.add_argument("--server", default=None, help="server config path (default: $PRM_SERVER_CONFIG)")
    smoke.set_defaults(func=_cmd_smoke_llm)

    serve = sub.add_parser("serve", help="run the web server (webhook, API, dashboard)")
    serve.add_argument("--host", default="0.0.0.0")  # noqa: S104 - a server; the bind address is the operator's
    serve.add_argument("--port", type=int, default=8000)
    serve.add_argument("--reload", action="store_true")
    serve.set_defaults(func=_cmd_serve)

    repo = sub.add_parser("repo", help="manage registered target repos")
    repo_sub = repo.add_subparsers(dest="repo_command", required=True)
    repo_add = repo_sub.add_parser("add", help="register a repo and create its traders")
    repo_add.add_argument("name", type=_repo_name, help="owner/name")
    repo_add.set_defaults(func=_cmd_repo_add)

    market = sub.add_parser("market", help="run the pipeline on a PR without a webhook")
    market.add_argument("pr", type=_pr_ref, help="owner/repo#N")
    market.set_defaults(func=_cmd_market)

    settle = sub.add_parser("settle", help="retired: settlement is post-merge now (prints why, exits 2)")
    settle.add_argument("--target", default=None, help="ignored (kept for old scripts)")
    settle.set_defaults(func=_cmd_settle)

    demo = sub.add_parser("demo", help="demo markets: bug | good | reset")
    demo.add_argument("action", choices=["bug", "good", "reset"])
    demo.add_argument("--local", action="store_true", help="run offline on a local target (no GitHub)")
    demo.add_argument("--target", default=None, help="local target root")
    demo.add_argument("--repo", type=_repo_name, default=None, help="owner/name to record the market under")
    demo.add_argument("--patch", default=None, help="patch file (default: <target>/../patches/<kind>.patch)")
    demo.add_argument("--yes", action="store_true", help="confirm `demo reset`")
    demo.set_defaults(func=_cmd_demo)

    league = sub.add_parser("league", help="run the league on a local target (aggregates only)")
    league.add_argument("--target", required=True, help="local target root")
    league.add_argument("--server", default=None, help="server config path (default: $PRM_SERVER_CONFIG)")
    league.add_argument("--n", type=int, default=None, help="number of mutants (default: [league].n)")
    league.add_argument("--seed", type=int, default=None)
    league.add_argument("--bug-fraction", type=float, default=None)
    league.add_argument("--concurrency", type=int, default=None)
    league.add_argument("--out", default=None, help="cache dir (default: $PRM_LEAGUE_DIR/<target>)")
    league.add_argument("--docs", default="docs/LEAGUE.md", help="markdown report path ('' to skip)")
    league.set_defaults(func=_cmd_league)
    return parser


DOTENV_FILE = ".env"
NO_DOTENV_ENV = "PRM_NO_DOTENV"


def load_local_env(cwd: Path | None = None) -> bool:
    """Load ``./.env`` into ``os.environ`` for a command-line run; variables already set win (override=False).

    Values are never printed or logged. Skipped when ``PRM_NO_DOTENV`` is set. True if a file was loaded.
    """
    if os.environ.get(NO_DOTENV_ENV):
        return False
    from dotenv import load_dotenv

    path = (cwd or Path.cwd()) / DOTENV_FILE
    return path.is_file() and load_dotenv(path, override=False)


def main(argv: Sequence[str] | None = None) -> int:
    if argv is None:  # a real `prm …` run; tests pass argv and never see the developer's secrets
        load_local_env()
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
