"""League runner: parallel cached beliefs + oracle, then a sequential, deterministic replay (real engine)."""

from __future__ import annotations

import asyncio
import difflib
import shutil
from dataclasses import dataclass
from pathlib import Path

import pytest

from prm.config import RuntimeEnv, ServerConfig, TargetConfig
from prm.league.oracle import run_tests
from prm.league.run import CachingProvider, default_out_dir, run_league
from prm.pipeline import apply_patch
from prm.traders.roster import make_engine_provider
from prm.types import SettlementResult
from tests.league.sample import BUG_PATCH, IGNORE, copy_sample

N_MUTANTS = 6


@dataclass(frozen=True)
class TinyMutant:
    id: str
    diff: str
    description: str
    head_dir: Path
    kind: str
    operators: tuple[str, ...] = ()


def _tree_diff(base: Path, head: Path) -> str:
    out: list[str] = []
    for f in sorted(p for p in head.rglob("*.py") if p.is_file()):
        rel = f.relative_to(head).as_posix()
        old = (base / rel).read_text(encoding="utf-8").splitlines(keepends=True)
        new = f.read_text(encoding="utf-8").splitlines(keepends=True)
        if old != new:
            out.append(f"diff --git a/{rel} b/{rel}\n")
            out.extend(difflib.unified_diff(old, new, f"a/{rel}", f"b/{rel}"))
    return "".join(out)


def _mutants(base: Path, root: Path) -> list[TinyMutant]:
    out = []
    for i in range(N_MUTANTS):
        head = root / f"m{i}"
        shutil.copytree(base, head, ignore=IGNORE)
        bug = i % 3 == 0
        if bug:
            apply_patch(head, BUG_PATCH)
        with (head / "notes_app" / "auth.py").open("a", encoding="utf-8") as fh:
            fh.write(f"\n# tidy note {i}\n")
        kind = "bug" if bug else "benign"
        out.append(TinyMutant(f"m{i}", _tree_diff(base, head), "Tidy request handling", head, kind))
    return out


class Counting:
    def __init__(self, inner: object) -> None:
        self.inner, self.calls = inner, 0

    async def get_belief(self, spec, bundle, escalation=None):  # type: ignore[no-untyped-def]
        self.calls += 1
        if escalation is None:
            return await self.inner.get_belief(spec, bundle)  # type: ignore[attr-defined]
        return await self.inner.get_belief(spec, bundle, escalation=escalation)  # type: ignore[attr-defined]


@pytest.fixture
def setup(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, list[TinyMutant], ServerConfig]:
    monkeypatch.setenv("PRM_FAKE_LLM", "1")
    monkeypatch.delenv("PRM_REPLAY_BELIEFS", raising=False)
    base = copy_sample(tmp_path)
    return base, _mutants(base, tmp_path / "mutants"), ServerConfig()


def _run(
    base: Path, mutants: list[TinyMutant], out: Path, cfg: ServerConfig, provider, oracle=run_tests
) -> dict:  # type: ignore[no-untyped-def]
    return asyncio.run(
        run_league(
            base, mutants, out_dir=out, cfg=cfg, provider=provider, concurrency=4, warmup=2, oracle=oracle
        )
    )


def test_league_records_outcomes_and_caches(setup, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    base, mutants, cfg = setup
    out = tmp_path / "out"
    oracle_calls: list[Path] = []

    def counting_oracle(head: Path, target: TargetConfig, root: Path) -> SettlementResult:
        oracle_calls.append(head)
        return run_tests(head, target, root)

    first = Counting(make_engine_provider(cfg, RuntimeEnv.from_env()))
    rep1 = _run(base, mutants, out, cfg, first, counting_oracle)
    assert first.calls >= N_MUTANTS * len(cfg.traders)
    assert len(oracle_calls) == N_MUTANTS
    assert rep1["n_markets"] == N_MUTANTS
    assert rep1["confusion"]["bug"]["NO"] == 2  # the auth bug fails the hidden tests
    assert rep1["confusion"]["benign"]["YES"] == 4
    assert rep1["n_scored"] == N_MUTANTS
    assert rep1["beliefs"] == "injected"
    assert rep1["cost"]["provider_calls_this_run"] == first.calls
    assert all(len(series) == N_MUTANTS for series in rep1["bankrolls"].values())
    assert (out / "report.json").is_file() and (out / "beliefs.jsonl").is_file()
    assert (out / "settlements.jsonl").is_file() and (out / "league.db").is_file()

    second = Counting(make_engine_provider(cfg, RuntimeEnv.from_env()))
    rep2 = _run(base, mutants, out, cfg, second, counting_oracle)
    assert second.calls == 0  # every belief, escalations included, comes from beliefs.jsonl
    assert len(oracle_calls) == N_MUTANTS  # and every settlement from settlements.jsonl
    for key in ("scores", "calibration", "gate_economics", "confusion", "bankrolls"):
        assert rep2[key] == rep1[key]  # sequential replay with a seeded RNG is deterministic


def test_fake_provider_label_and_infra_error_is_unscored(setup, tmp_path: Path) -> None:  # type: ignore[no-untyped-def]
    base, mutants, cfg = setup

    def broken(head: Path, target: TargetConfig, root: Path) -> SettlementResult:
        return SettlementResult.infra_error("boom")

    rep = asyncio.run(run_league(base, mutants[:2], out_dir=tmp_path / "o", cfg=cfg, oracle=broken))
    assert rep["beliefs"] == "fake"
    assert rep["n_scored"] == 0
    assert sum(row["error"] for row in rep["confusion"].values()) == 2
    assert not (tmp_path / "o" / "settlements.jsonl").exists()  # infra errors are not cached


def test_caching_provider_skips_abstentions(tmp_path: Path, setup) -> None:  # type: ignore[no-untyped-def]
    class Abstains:
        async def get_belief(self, spec, bundle, escalation=None):  # type: ignore[no-untyped-def]
            from prm.types import BeliefResult

            return BeliefResult(spec.name, None, "", [], spec.model, 0, 0, 0, 0.0, "TimeoutError")

    base, mutants, cfg = setup
    from prm.config import load_target_config
    from prm.league.run import _bundle

    cache = CachingProvider(Abstains(), tmp_path / "b.jsonl", "fake")
    bundle = _bundle(mutants[0], base, load_target_config(base))
    asyncio.run(cache.get_belief(cfg.traders[0], bundle))
    asyncio.run(cache.get_belief(cfg.traders[0], bundle))
    assert cache.calls == 2
    assert not (tmp_path / "b.jsonl").exists()


def test_cli_league_publishes(setup, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys) -> None:  # type: ignore[no-untyped-def]
    import prm.league.mutate as mutate
    from prm import cli

    base, mutants, _ = setup
    seen: dict = {}

    def fake_generate(target_dir, out_dir, *, n, seed, bug_fraction, ctx):  # type: ignore[no-untyped-def]
        seen.update(n=n, seed=seed, bug_fraction=bug_fraction)
        return mutants[:n]

    monkeypatch.setattr(mutate, "generate_mutants", fake_generate, raising=False)
    monkeypatch.setenv("PRM_DB", str(tmp_path / "main.db"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    docs = tmp_path / "LEAGUE.md"
    argv = ["league", "--target", str(base), "--n", "3", "--out", str(tmp_path / "o"), "--docs", str(docs)]
    assert cli.main(argv) == 0
    assert seen == {"n": 3, "seed": 7, "bug_fraction": 0.4}
    assert "chosen in hindsight" in docs.read_text(encoding="utf-8")
    printed = capsys.readouterr().out
    assert "league: 3 markets" in printed and "gate 0.90" in printed

    from fastapi.testclient import TestClient

    from prm.server import create_app

    resp = TestClient(create_app(cfg=ServerConfig())).get("/api/league/report")
    assert resp.status_code == 200 and resp.json()["n_markets"] == 3


def test_default_out_dir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PRM_LEAGUE_DIR", str(tmp_path))
    assert default_out_dir("t") == tmp_path / "t"
    monkeypatch.delenv("PRM_LEAGUE_DIR")
    assert default_out_dir("t") == Path.cwd() / ("." + "league") / "t"
