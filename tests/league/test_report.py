"""League report: hand-made records -> known Brier, baselines, calibration, gate economics, flags."""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest
from sqlalchemy import select

from prm.db import make_session_factory
from prm.league.report import (
    HINDSIGHT,
    IN_SAMPLE,
    MarketRecord,
    bootstrap_ci,
    brier,
    build_report,
    log_loss,
    majority,
    publish,
    render_markdown,
)
from prm.models import LeagueRun


def _rec(i: int, price: float | None, outcome: str | None, a: float, b: float, kind: str, gate: str,
         esc: int = 0) -> MarketRecord:  # fmt: skip
    return MarketRecord(
        i, price, gate, esc, outcome, {"a": a, "b": b}, 0.01, kind, {"a": 100.0 + i, "b": 100.0}
    )


RECORDS = [
    _rec(0, 0.90, "YES", 0.8, 0.9, "benign", "auto"),
    _rec(1, 0.20, "NO", 0.3, 0.1, "bug", "blocked", esc=1),
    _rec(2, 0.95, "NO", 0.6, 0.4, "bug", "auto"),
    _rec(3, 0.70, "YES", 0.7, 0.5, "benign", "no_consensus", esc=2),
    _rec(4, None, "YES", 0.5, 0.5, "benign", "void"),
]


@pytest.fixture(scope="module")
def report() -> dict:
    return build_report(RECORDS, warmup=2, params={"target": "t", "seed": 7}, belief_source="fake")


def test_brier_and_baselines(report: dict) -> None:
    s = report["scores"]["all"]
    assert s["market"]["n"] == 4  # the VOID market has no price: not scored
    assert s["market"]["brier"] == pytest.approx(0.2606, abs=1e-4)
    assert s["average"]["brier"] == pytest.approx(0.1181, abs=1e-4)
    assert s["majority_vote"]["brier"] == pytest.approx(0.125, abs=1e-4)
    assert s["best_single"]["trader"] == "b"
    assert s["best_single"]["brier"] == pytest.approx(0.1075, abs=1e-4)
    assert s["best_single"]["flag"] == HINDSIGHT
    assert s["base_rate"]["flag"] == IN_SAMPLE
    assert s["base_rate"]["rate"] == 0.5
    assert s["base_rate"]["brier"] == pytest.approx(0.25)
    assert report["scores"]["post_warmup"]["market"]["n"] == 2
    assert report["n_markets"] == 5 and report["n_scored"] == 4


def test_ci_shape(report: dict) -> None:
    m = report["scores"]["all"]["market"]
    lo, hi = m["brier_ci"]
    assert lo <= m["brier"] <= hi
    assert len(m["log_loss_ci"]) == 2
    assert bootstrap_ci([], brier) is None
    assert bootstrap_ci([(0.5, 1)] * 3, brier) == [0.25, 0.25]


def test_calibration_bins(report: dict) -> None:
    bins = report["calibration"]
    assert len(bins) == 10
    assert [b["n"] for b in bins] == [0, 0, 1, 0, 0, 0, 0, 1, 0, 2]
    assert bins[9]["mean_p"] == pytest.approx(0.925)
    assert bins[9]["observed"] == 0.5
    assert bins[0]["mean_p"] is None


def test_gate_economics(report: dict) -> None:
    g = report["gate_economics"]
    rows = {r["threshold"]: r for r in g["thresholds"]}
    assert set(rows) == {0.80, 0.85, 0.90, 0.95}
    assert rows[0.90]["auto_pct"] == 50.0
    assert rows[0.90]["escaped_defect_rate"] == 50.0
    assert rows[0.90]["false_reject_rate"] == 50.0
    assert rows[0.95]["auto_pct"] == 25.0
    assert rows[0.95]["escaped_defect_rate"] == 100.0
    assert rows[0.95]["false_reject_rate"] == 66.7
    assert g["engine"] == {"n": 5, "escalated_pct": 40.0, "no_consensus_pct": 20.0, "void_pct": 20.0,
                           "auto_merged_pct": 40.0}  # fmt: skip


def test_confusion_cost_and_bankrolls(report: dict) -> None:
    assert report["confusion"] == {
        "benign": {"YES": 3, "NO": 0, "error": 0},
        "bug": {"YES": 0, "NO": 2, "error": 0},
    }
    assert report["cost"]["total_usd"] == pytest.approx(0.05)
    assert report["cost"]["per_market_usd"] == pytest.approx(0.01)
    assert report["bankrolls"]["a"] == [100.0, 101.0, 102.0, 103.0, 104.0]


def test_scores_and_majority_helpers() -> None:
    assert majority([0.6, 0.7, 0.2]) == 1.0
    assert majority([0.6, 0.4]) == 0.5
    assert majority([0.1, 0.2, 0.9]) == 0.0
    assert log_loss([(0.0, 1)]) == pytest.approx(-math.log(1e-3))
    assert brier([]) is None


def test_markdown_flags_and_aggregates_only(report: dict) -> None:
    md = render_markdown(report)
    assert "chosen in hindsight" in md and "in-sample" in md
    assert "Beliefs: **fake**" in md
    assert "| Market (closing price) | 4 |" in md
    assert "diff --git" not in md


def test_publish_writes_docs_and_db_row(tmp_path: Path, report: dict) -> None:
    sf = make_session_factory(f"sqlite:///{tmp_path / 'main.db'}")
    docs = tmp_path / "docs" / "LEAGUE.md"
    row_id = publish(report, docs_path=docs, session_factory=sf, repo_label="league/t")
    assert docs.read_text(encoding="utf-8").startswith("# League report")
    with sf() as s:
        row = s.scalar(select(LeagueRun).where(LeagueRun.id == row_id))
        assert row is not None and row.repo_label == "league/t"
        assert json.loads(row.report_json or "{}")["n_markets"] == 5
        assert json.loads(row.params_json)["seed"] == 7
