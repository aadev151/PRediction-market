"""WP B. Replay cached beliefs keyed by (trader, sha256(diff)) (PRM_REPLAY_BELIEFS). PLAN §6.4.

The JSONL file is the offline demo fallback and the league's belief cache format. Each line is a
``BeliefRecord``. Invalid lines are skipped (and counted); a miss abstains, or asks the fallback provider.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Iterable
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from prm.config import TraderSpec
from prm.traders.base import BeliefProvider, abstain
from prm.types import BeliefResult, PRBundle, TraderContext


class BeliefRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    trader: str
    diff_sha256: str
    p_yes: float | None
    rationale: str = ""
    key_risks: list[str] = []
    model: str = "replay"
    input_tokens: int = 0
    output_tokens: int = 0
    latency_ms: int = 0
    cost_usd: float = 0.0
    error: str | None = None


def diff_key(diff: str) -> str:
    return hashlib.sha256(diff.encode("utf-8")).hexdigest()


def to_record(result: BeliefResult, diff: str) -> BeliefRecord:
    return BeliefRecord(diff_sha256=diff_key(diff), **dataclasses.asdict(result))


def to_result(rec: BeliefRecord) -> BeliefResult:
    return BeliefResult(**rec.model_dump(exclude={"diff_sha256"}))


def append_beliefs(path: Path, diff: str, results: Iterable[BeliefResult]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for r in results:
            fh.write(to_record(r, diff).model_dump_json() + "\n")


def load_beliefs(path: Path) -> tuple[dict[tuple[str, str], BeliefResult], int]:
    """``{(trader, diff_sha256): belief}`` (last line wins) and the number of invalid lines skipped."""
    table: dict[tuple[str, str], BeliefResult] = {}
    skipped = 0
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                rec = BeliefRecord.model_validate(json.loads(line))
                table[(rec.trader, rec.diff_sha256)] = to_result(rec)
            except (ValueError, ValidationError):
                skipped += 1
    return table, skipped


class ReplayProvider:
    def __init__(self, path: Path, fallback: BeliefProvider | None = None) -> None:
        self.path = Path(path)
        self.fallback = fallback
        self._table: dict[tuple[str, str], BeliefResult] | None = None
        self.load_error: str | None = None
        self.skipped = 0

    def _lookup(self, trader: str, diff: str) -> BeliefResult | None:
        if self._table is None:
            try:
                self._table, self.skipped = load_beliefs(self.path)
            except OSError as exc:
                self._table, self.load_error = {}, f"cannot read replay file: {type(exc).__name__}"
        return self._table.get((trader, diff_key(diff)))

    async def belief(self, spec: TraderSpec, ctx: TraderContext, bundle: PRBundle) -> BeliefResult:
        hit = self._lookup(spec.name, bundle.diff)
        if hit is not None:
            return hit
        if self.fallback is not None:
            return await self.fallback.belief(spec, ctx, bundle)
        return abstain(spec.name, "replay", self.load_error or "no replayed belief for this trader and diff")
