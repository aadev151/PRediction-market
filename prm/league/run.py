"""WP G. Parallel beliefs + settlements (cached), then sequential replay through MarketEngine. PLAN §6.10.

1. Parallel (``asyncio.Semaphore(concurrency)``): each mutant's round-1 beliefs through a caching provider
   (``beliefs.jsonl``) and its hidden-test result (``settlements.jsonl``, the oracle in a worker thread).
2. Sequential replay through a real ``MarketEngine`` on a fresh SQLite (``league.db``) with a seeded RNG, so
   bankrolls evolve in order and re-runs are deterministic. Escalation beliefs hit the same cache lazily.
3. ``report.build_report`` -> ``out_dir/report.json``. Everything in ``out_dir`` is private (R-LG-6).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import random
from collections.abc import Callable, Sequence
from dataclasses import asdict
from pathlib import Path
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from prm.config import RuntimeEnv, ServerConfig, TargetConfig, TraderSpec, load_target_config
from prm.db import make_session_factory
from prm.league.oracle import run_tests
from prm.league.report import MarketRecord, build_report
from prm.market.engine import REFUNDED, VOIDED, BeliefProvider, MarketEngine
from prm.models import Market, Trader
from prm.types import BeliefResult, EscalationContext, PRBundle, SettlementResult, State

Oracle = Callable[[Path, TargetConfig, Path], SettlementResult]
LEAGUE_DIR_ENV = "PRM_LEAGUE_DIR"


class MutantLike(Protocol):
    """What ``run_league`` needs from a mutant (``prm.league.mutate.Mutant`` satisfies it)."""

    @property
    def id(self) -> str: ...
    @property
    def diff(self) -> str: ...
    @property
    def description(self) -> str: ...
    @property
    def head_dir(self) -> Path: ...
    @property
    def kind(self) -> str: ...


def default_out_dir(target_name: str) -> Path:
    """``$PRM_LEAGUE_DIR/<target>``, else the gitignored league dot-directory under the cwd."""
    base = os.environ.get(LEAGUE_DIR_ENV) or str(Path.cwd() / ("." + "league"))
    return Path(base) / target_name


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def belief_key(source: str, spec: TraderSpec, diff: str, escalation: EscalationContext | None) -> str:
    """(provider kind, trader, model, sha256(diff), escalation round or 0, sha256(previous + peers))."""
    payload = ""
    if escalation is not None:
        peers = [[p.trader, p.rationale, list(p.key_risks)] for p in escalation.peers]
        payload = json.dumps(
            {"prev": escalation.previous_p_yes, "why": escalation.previous_rationale, "peers": peers},
            sort_keys=True,
        )
    rnd = 0 if escalation is None else escalation.round
    return "|".join([source, spec.name, spec.model, _sha(diff), str(rnd), _sha(payload)])


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            row = json.loads(line)
        except ValueError:
            continue  # a torn last line from an interrupted run
        if isinstance(row, dict):
            rows.append(row)
    return rows


def _append_jsonl(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, sort_keys=True) + "\n")


class CachingProvider:
    """A ``BeliefProvider`` that persists every non-abstaining belief to ``path`` (abstentions retry)."""

    def __init__(self, inner: BeliefProvider, path: Path, source: str) -> None:
        self.inner, self.path, self.source = inner, path, source
        self.calls = 0
        self._cache: dict[str, BeliefResult] = {}
        for row in _read_jsonl(path):
            try:
                self._cache[row["key"]] = BeliefResult(**row["belief"])
            except (KeyError, TypeError, ValueError):
                continue

    async def get_belief(
        self, spec: TraderSpec, bundle: PRBundle, escalation: EscalationContext | None = None
    ) -> BeliefResult:
        key = belief_key(self.source, spec, bundle.diff, escalation)
        hit = self._cache.get(key)
        if hit is not None:
            return hit
        self.calls += 1
        if escalation is None:
            br = await self.inner.get_belief(spec, bundle)
        else:
            br = await self.inner.get_belief(spec, bundle, escalation=escalation)
        if br.p_yes is not None:
            self._cache[key] = br
            _append_jsonl(self.path, {"key": key, "belief": asdict(br)})
        return br


def _settle_key(m: MutantLike) -> str:
    return f"{m.id}|{_sha(m.diff)}"


def _load_settlements(path: Path) -> dict[str, SettlementResult]:
    out: dict[str, SettlementResult] = {}
    for row in _read_jsonl(path):
        try:
            if row["outcome"] in ("YES", "NO"):
                out[row["key"]] = SettlementResult(
                    row["outcome"], row["passed"], row["total"], [], row["ms"], None
                )
        except (KeyError, TypeError, ValueError):
            continue
    return out


def _bundle(m: MutantLike, target_dir: Path, target: TargetConfig) -> PRBundle:
    return PRBundle(
        source="league", repo=f"league/{target.target.name}", pr_number=None, head_sha=None, base_sha=None,
        diff=m.diff, description=m.description, base_dir=target_dir, head_dir=Path(m.head_dir), target=target,
    )  # fmt: skip


def _gate_kind(m: Market) -> str:
    if m.resolution == VOIDED or m.final_price is None:
        return "void"
    desc = m.gate_description or ""
    if m.gate_state == "success":
        return "auto"
    return "no_consensus" if desc.startswith("no consensus") else "blocked"


def _record(
    sf: sessionmaker[Session], market_id: int, repo_id: int, index: int, round1: dict[str, float | None],
    kind: str, result: SettlementResult,
) -> MarketRecord:  # fmt: skip
    with sf() as s:
        m = s.get(Market, market_id)
        assert m is not None  # noqa: S101 - created a line earlier
        rolls = {
            t.name: t.bankroll
            for t in s.scalars(select(Trader).where(Trader.repo_id == repo_id, Trader.kind == "agent"))
        }
        settled = State(m.state) == State.SETTLED or m.resolution == REFUNDED
        return MarketRecord(
            index=index, price=m.final_price, gate=_gate_kind(m), escalations=m.escalations or 0,
            outcome=result.outcome if settled else None, beliefs=round1, cost_usd=m.cost_usd or 0.0,
            kind=kind, bankrolls=rolls,
        )  # fmt: skip


def _belief_source(provider: BeliefProvider | None, env: RuntimeEnv) -> str:
    if provider is not None:
        return "injected"
    if env.replay_beliefs is not None:
        return "replay"
    return "fake" if env.fake_llm else "real"


async def run_league(
    target_dir: Path,
    mutants: Sequence[MutantLike],
    *,
    out_dir: Path,
    cfg: ServerConfig,
    provider: BeliefProvider | None = None,
    concurrency: int = 8,
    warmup: int | None = None,
    oracle: Oracle = run_tests,
    params: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Run the league and return the report (also written to ``out_dir/report.json``)."""
    target_dir = Path(target_dir).resolve()
    target = load_target_config(target_dir)
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    env = RuntimeEnv.from_env()
    source = _belief_source(provider, env)
    if provider is None:
        from prm.traders.roster import make_engine_provider

        provider = make_engine_provider(cfg, env)
    cache = CachingProvider(provider, out_dir / "beliefs.jsonl", source)
    settle_path = out_dir / "settlements.jsonl"
    known = _load_settlements(settle_path)
    bundles = [_bundle(m, target_dir, target) for m in mutants]
    results: list[SettlementResult] = [SettlementResult.infra_error("not run")] * len(mutants)
    sem = asyncio.Semaphore(concurrency)

    async def settle(i: int, m: MutantLike) -> None:
        key = _settle_key(m)
        if key in known:
            results[i] = known[key]
            return
        r = await asyncio.to_thread(oracle, Path(m.head_dir), target, target_dir)
        results[i] = r
        if r.error is None:
            known[key] = r
            _append_jsonl(settle_path, {"key": key, "outcome": r.outcome, "passed": r.holdouts_passed,
                                        "total": r.holdouts_total, "ms": r.duration_ms})  # fmt: skip

    async def prepare(i: int, m: MutantLike) -> None:
        async with sem:
            await asyncio.gather(settle(i, m), *(cache.get_belief(sp, bundles[i]) for sp in cfg.traders))

    await asyncio.gather(*(prepare(i, m) for i, m in enumerate(mutants)))

    db = out_dir / "league.db"
    for suffix in ("", "-wal", "-shm", "-journal"):
        Path(f"{db}{suffix}").unlink(missing_ok=True)
    sf = make_session_factory(f"sqlite:///{db}")
    engine = MarketEngine(sf, cfg, cache, rng=random.Random(cfg.league.seed))  # noqa: S311 - trade order
    repo_label = f"league/{target.target.name}"
    repo_id = engine.ensure_repo(repo_label)
    records: list[MarketRecord] = []
    n_traders = len(cfg.traders)
    for i, m in enumerate(mutants):
        bundle = bundles[i]
        market_id = engine.create(repo_id, bundle)
        beliefs = await engine.run_agents(market_id, bundle)
        round1 = {sp.name: br.p_yes for sp, br in zip(cfg.traders, beliefs[:n_traders], strict=True)}
        with sf() as s:
            state = State(s.get(Market, market_id).state)  # type: ignore[union-attr]
        if state == State.TRADING:
            engine.close(market_id)
            engine.gate(market_id, target.gate)
            engine.settle(market_id, results[i])
        records.append(_record(sf, market_id, repo_id, i, round1, m.kind, results[i]))

    run_params = {
        "target": target.target.name, "repo": repo_label, "n": len(mutants), "seed": cfg.league.seed,
        "concurrency": concurrency, **(params or {}),
    }  # fmt: skip
    report = build_report(
        records, warmup=cfg.league.warmup if warmup is None else warmup, params=run_params,
        belief_source=source, provider_calls=cache.calls,
    )  # fmt: skip
    (out_dir / "report.json").write_text(json.dumps(report, indent=2, sort_keys=True), encoding="utf-8")
    return report
