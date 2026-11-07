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
from typing import TYPE_CHECKING

from prm import __version__
from prm.config import ConfigError, RuntimeEnv, load_server_config, load_target_config
from prm.types import SettlementResult

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


def format_settlement(result: SettlementResult) -> str:
    """Counts only (R-SET-5), e.g. ``holdouts 11/12 passed · red team: 0 high · outcome NO``."""
    high = sum(1 for f in result.findings if f.severity == "high" and f.confirmed)
    passed, total = result.holdouts_passed, result.holdouts_total
    return f"holdouts {passed}/{total} passed · red team: {high} high · outcome {result.outcome}"


def _cmd_settle(args: argparse.Namespace) -> int:
    from prm import pipeline

    root = Path(args.target).resolve()
    try:
        target = pipeline.absolute_holdouts(load_target_config(root), root)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1
    result = pipeline.default_settler(root, target)
    if result.error:
        print(f"settlement error: {result.error}", file=sys.stderr)
        return 2
    print(format_settlement(result))
    return 0 if result.outcome == "YES" else 1


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
        target, patch, repo=args.repo, description=f"demo: {patch.name}", deps=deps
    )
    try:
        market_id = asyncio.run(run)
    except (ConfigError, pipeline.PatchError) as exc:
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

    settle = sub.add_parser("settle", help="settle a local target's tree; prints counts only")
    settle.add_argument("--target", required=True, help="target root")
    settle.set_defaults(func=_cmd_settle)

    demo = sub.add_parser("demo", help="demo markets: bug | good | reset")
    demo.add_argument("action", choices=["bug", "good", "reset"])
    demo.add_argument("--local", action="store_true", help="run offline on a local target (no GitHub)")
    demo.add_argument("--target", default=None, help="local target root")
    demo.add_argument("--repo", type=_repo_name, default=None, help="owner/name to record the market under")
    demo.add_argument("--patch", default=None, help="patch file (default: <target>/../patches/<kind>.patch)")
    demo.add_argument("--yes", action="store_true", help="confirm `demo reset`")
    demo.set_defaults(func=_cmd_demo)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
