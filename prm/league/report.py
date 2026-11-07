"""WP G. Brier, log loss, bootstrap CI, baselines, calibration, gate economics (aggregates). PLAN §6.10.

Pure functions over per-market records, plus ``publish`` (docs markdown + a ``league_runs`` row). Every market
with a price and an outcome is scored against its hidden test result (R-LG-2), whatever the gate did. Only
aggregates leave this module: no mutant ids, diffs or per-mutant labels (R-LG-6).
"""

from __future__ import annotations

import json
import math
import random
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from prm.models import LeagueRun

THRESHOLDS = (0.80, 0.85, 0.90, 0.95)
LOG_EPS = 1e-3  # log loss clips p to [0.001, 0.999] so a 0/1 baseline (majority vote) stays finite
N_BOOT = 1000
BOOT_SEED = 12345
HINDSIGHT = "chosen in hindsight"
IN_SAMPLE = "in-sample"

Pair = tuple[float, int]


@dataclass(frozen=True)
class MarketRecord:
    """One replayed league market. ``price`` is None if the market VOIDed before closing; ``outcome`` is None
    on an oracle infra error. ``kind`` (the mutation label) is only ever used in aggregate."""

    index: int
    price: float | None
    gate: str  # "auto" | "blocked" | "no_consensus" | "void"
    escalations: int
    outcome: str | None
    beliefs: dict[str, float | None]
    cost_usd: float
    kind: str
    bankrolls: dict[str, float] = field(default_factory=dict)


# ------------------------------------------------------------------------------------------------ scores


def brier(pairs: Sequence[Pair]) -> float | None:
    return sum((p - y) ** 2 for p, y in pairs) / len(pairs) if pairs else None


def log_loss(pairs: Sequence[Pair]) -> float | None:
    if not pairs:
        return None
    total = 0.0
    for p, y in pairs:
        q = min(max(p, LOG_EPS), 1 - LOG_EPS)
        total -= math.log(q) if y else math.log(1 - q)
    return total / len(pairs)


def bootstrap_ci(
    pairs: Sequence[Pair], metric: Callable[[Sequence[Pair]], float | None], *, seed: int = BOOT_SEED
) -> list[float] | None:
    """95% percentile CI over ``N_BOOT`` resamples with a fixed seed. None for an empty sample."""
    if not pairs:
        return None
    rng = random.Random(seed)  # noqa: S311 - statistics, not security
    n = len(pairs)
    stats = sorted(metric([pairs[rng.randrange(n)] for _ in range(n)]) or 0.0 for _ in range(N_BOOT))
    return [round(stats[int(0.025 * N_BOOT)], 4), round(stats[int(0.975 * N_BOOT) - 1], 4)]


def _r(x: float | None) -> float | None:
    return None if x is None else round(x, 4)


def score(pairs: Sequence[Pair], flag: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {
        "n": len(pairs),
        "brier": _r(brier(pairs)),
        "brier_ci": bootstrap_ci(pairs, brier),
        "log_loss": _r(log_loss(pairs)),
        "log_loss_ci": bootstrap_ci(pairs, log_loss),
    }
    if flag:
        out["flag"] = flag
    return out


def _y(r: MarketRecord) -> int:
    return 1 if r.outcome == "YES" else 0


def scored(records: Sequence[MarketRecord]) -> list[MarketRecord]:
    """Markets with a closing price and an oracle outcome: every one is scored, merged or not (R-LG-2)."""
    return [r for r in records if r.price is not None and r.outcome is not None]


def _responders(r: MarketRecord) -> list[float]:
    return [p for p in r.beliefs.values() if p is not None]


def majority(ps: Sequence[float]) -> float:
    """Share of beliefs above 0.5: a majority -> 1, a minority -> 0, a tie -> 0.5."""
    share = sum(p > 0.5 for p in ps) / len(ps)
    return 1.0 if share > 0.5 else 0.0 if share < 0.5 else 0.5


def methods(records: Sequence[MarketRecord]) -> dict[str, Any]:
    """Market vs. simple average vs. majority vote vs. best single trader vs. base rate."""
    rs = [r for r in scored(records) if _responders(r)]
    market = [(r.price, _y(r)) for r in rs if r.price is not None]
    average = [(sum(ps) / len(ps), _y(r)) for r in rs for ps in [_responders(r)]]
    vote = [(majority(_responders(r)), _y(r)) for r in rs]
    best_name: str | None = None
    best_pairs: list[Pair] = []
    for name in sorted({name for r in rs for name in r.beliefs}):
        pairs = [(p, _y(r)) for r in rs if (p := r.beliefs.get(name)) is not None]
        b = brier(pairs)
        if b is not None and (best_name is None or b < (brier(best_pairs) or 0.0)):
            best_name, best_pairs = name, pairs
    rate = sum(_y(r) for r in rs) / len(rs) if rs else 0.0
    best = score(best_pairs, HINDSIGHT)
    best["trader"] = best_name
    return {
        "market": score(market),
        "average": score(average),
        "majority_vote": score(vote),
        "best_single": best,
        "base_rate": {**score([(rate, _y(r)) for r in rs], IN_SAMPLE), "rate": _r(rate)},
    }


def calibration(records: Sequence[MarketRecord]) -> list[dict[str, Any]]:
    """Market price in ten 0.1 bins: n, mean p and the observed YES rate."""
    bins: list[list[MarketRecord]] = [[] for _ in range(10)]
    for r in scored(records):
        bins[min(int((r.price or 0.0) * 10), 9)].append(r)
    out = []
    for i, rs in enumerate(bins):
        n = len(rs)
        out.append({
            "bin": [round(i / 10, 1), round((i + 1) / 10, 1)], "n": n,
            "mean_p": _r(sum(r.price or 0.0 for r in rs) / n) if n else None,
            "observed": _r(sum(_y(r) for r in rs) / n) if n else None,
        })  # fmt: skip
    return out


def _pct(k: int, n: int) -> float | None:
    return round(100 * k / n, 1) if n else None


def gate_economics(
    records: Sequence[MarketRecord], thresholds: Sequence[float] = THRESHOLDS
) -> dict[str, Any]:
    """Per threshold t over scored markets: % auto-merged (p >= t), escaped defects among them (outcome NO),
    false rejects among p < t (outcome YES). % escalated and % no-consensus: what the engine actually did."""
    rs = scored(records)
    n_all = len(records)
    engine = {
        "n": n_all,
        "escalated_pct": _pct(sum(r.escalations > 0 for r in records), n_all),
        "no_consensus_pct": _pct(sum(r.gate == "no_consensus" for r in records), n_all),
        "void_pct": _pct(sum(r.gate == "void" for r in records), n_all),
        "auto_merged_pct": _pct(sum(r.gate == "auto" for r in records), n_all),
    }
    rows = []
    for t in thresholds:
        auto = [r for r in rs if (r.price or 0.0) >= t]
        rejected = [r for r in rs if (r.price or 0.0) < t]
        escaped = sum(r.outcome == "NO" for r in auto)
        false_rejects = sum(r.outcome == "YES" for r in rejected)
        rows.append({
            "threshold": t, "n": len(rs), "auto_pct": _pct(len(auto), len(rs)),
            "n_auto": len(auto), "escaped_defect_rate": _pct(escaped, len(auto)),
            "n_rejected": len(rejected), "false_reject_rate": _pct(false_rejects, len(rejected)),
            "escalated_pct": engine["escalated_pct"], "no_consensus_pct": engine["no_consensus_pct"],
        })  # fmt: skip
    return {"thresholds": rows, "engine": engine}


def confusion(records: Sequence[MarketRecord]) -> dict[str, dict[str, int]]:
    """Mutation label vs. hidden-test outcome, counts only. A passing "bug" = a hidden-test coverage gap."""
    out: dict[str, dict[str, int]] = {}
    for r in records:
        row = out.setdefault(r.kind, {"YES": 0, "NO": 0, "error": 0})
        row[r.outcome or "error"] += 1
    return out


def bankrolls(records: Sequence[MarketRecord]) -> dict[str, list[float]]:
    """Each trader's bankroll after each market, in replay order (an aggregate series)."""
    names = sorted({name for r in records for name in r.bankrolls})
    return {name: [round(r.bankrolls.get(name, 0.0), 2) for r in records] for name in names}


def build_report(
    records: Sequence[MarketRecord],
    *,
    warmup: int,
    params: dict[str, Any],
    belief_source: str,
    provider_calls: int = 0,
) -> dict[str, Any]:
    post = [r for r in records if r.index >= warmup]
    total = sum(r.cost_usd for r in records)
    return {
        "params": params,
        "beliefs": belief_source,
        "n_markets": len(records),
        "n_scored": len(scored(records)),
        "warmup": warmup,
        "scores": {"all": methods(records), "post_warmup": methods(post)},
        "calibration": calibration(records),
        "gate_economics": gate_economics(records),
        "confusion": confusion(records),
        "cost": {
            "total_usd": round(total, 4),
            "per_market_usd": round(total / len(records), 4) if records else None,
            "provider_calls_this_run": provider_calls,
        },
        "bankrolls": bankrolls(records),
    }


# ------------------------------------------------------------------------------------------------ output


def _f(x: Any) -> str:
    return "–" if x is None else f"{x}"


def _ci(x: list[float] | None) -> str:
    return "–" if x is None else f"[{x[0]:.3f}, {x[1]:.3f}]"


_LABELS = {
    "market": "Market (closing price)",
    "average": "Simple average (round 1)",
    "majority_vote": "Majority vote",
    "best_single": "Best single trader",
    "base_rate": "Base rate",
}


def render_markdown(report: dict[str, Any]) -> str:
    p = report["params"]
    lines = [
        "# League report",
        "",
        "Generated by `prm league` (WP G). Aggregates only: no mutant diffs, ids or labels (R-LG-6).",
        "",
        f"- Target: `{p.get('target', '?')}` · markets: {report['n_markets']} · scored: {report['n_scored']}"
        f" · warm-up: {report['warmup']} · seed: {p.get('seed', '?')}"
        f" · bug fraction: {p.get('bug_fraction', '?')}",
        f"- Beliefs: **{report['beliefs']}** (fake = deterministic offline traders, not real LLM calls)",
        "- Ground truth: each mutant's hidden test result (DECISIONS #68), for every market, merged or not.",
        "",
    ]
    for subset, title in (("all", "All markets"), ("post_warmup", "After warm-up")):
        lines += [
            f"## Scores: {title}",
            "",
            "| Method | n | Brier | 95% CI | Log loss | 95% CI | Note |",
            "|---|---|---|---|---|---|---|",
        ]
        for key, label in _LABELS.items():
            s = report["scores"][subset][key]
            note = s.get("flag", "")
            if key == "best_single" and s.get("trader"):
                note = f"{s['trader']}, {note}"
            lines.append(
                f"| {label} | {s['n']} | {_f(s['brier'])} | {_ci(s['brier_ci'])} | {_f(s['log_loss'])} "
                f"| {_ci(s['log_loss_ci'])} | {note} |"
            )
        lines.append("")
    lines += ["## Calibration (market price)", "", "| Bin | n | Mean p | Observed YES |", "|---|---|---|---|"]
    for b in report["calibration"]:
        lines.append(
            f"| {b['bin'][0]:.1f}–{b['bin'][1]:.1f} | {b['n']} | {_f(b['mean_p'])} | {_f(b['observed'])} |"
        )
    g = report["gate_economics"]
    e = g["engine"]
    lines += [
        "",
        "## Gate economics",
        "",
        f"Engine, configured gate (n={e['n']}): auto-merged {_f(e['auto_merged_pct'])}%, escalated "
        f"{_f(e['escalated_pct'])}%, no consensus {_f(e['no_consensus_pct'])}%, void {_f(e['void_pct'])}%.",
        "",
        "| Threshold | n | % auto-merged | Escaped defects among auto | False rejects among rejected |",
        "|---|---|---|---|---|",
    ]
    for row in g["thresholds"]:
        lines.append(
            f"| {row['threshold']:.2f} | {row['n']} | {_f(row['auto_pct'])}% "
            f"| {_f(row['escaped_defect_rate'])}% (n={row['n_auto']}) "
            f"| {_f(row['false_reject_rate'])}% (n={row['n_rejected']}) |"
        )
    lines += [
        "",
        "## Mutation label vs. hidden-test outcome (counts)",
        "",
        "| Label | YES | NO | error |",
        "|---|---|---|---|",
    ]
    for kind, row in sorted(report["confusion"].items()):
        lines.append(f"| {kind} | {row['YES']} | {row['NO']} | {row['error']} |")
    c = report["cost"]
    lines += [
        "",
        "A `bug` mutant that passes the hidden tests is a coverage gap in those tests, not a market error.",
        "",
        "## Cost",
        "",
        f"Total ${c['total_usd']:.4f} · ${_f(c['per_market_usd'])}/market · "
        f"new provider calls this run: {c['provider_calls_this_run']}",
        "",
        "## Final bankrolls",
        "",
        "| Trader | Final bankroll |",
        "|---|---|",
    ]
    for name, series in report["bankrolls"].items():
        lines.append(f"| {name} | {series[-1] if series else '–'} |")
    return "\n".join(lines) + "\n"


def publish(
    report: dict[str, Any],
    *,
    docs_path: Path | None,
    session_factory: sessionmaker[Session] | None,
    repo_label: str,
) -> int | None:
    """Write the markdown and a ``league_runs`` row (served by ``GET /api/league/report``). Returns its id."""
    if docs_path is not None:
        docs_path.parent.mkdir(parents=True, exist_ok=True)
        docs_path.write_text(render_markdown(report), encoding="utf-8")
    if session_factory is None:
        return None
    with session_factory.begin() as s:
        row = LeagueRun(
            repo_label=repo_label, params_json=json.dumps(report["params"]), report_json=json.dumps(report)
        )
        s.add(row)
        s.flush()
        return row.id
