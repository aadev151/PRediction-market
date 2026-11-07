"""`prm` command line (PLAN §6.15).

Phase 0 ships `prm --version` and `prm validate`; WP B adds `prm smoke-llm`.
WP E adds serve, repo, market, settle, league and demo.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from collections.abc import Sequence

from prm import __version__
from prm.config import ConfigError, load_server_config, load_target_config


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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
