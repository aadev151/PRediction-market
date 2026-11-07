"""WP B. Replay cached beliefs (PRM_REPLAY_BELIEFS). PLAN §6.4.

Key: ``(trader, sha256(diff), escalation_round, sha256(what the trader was shown))``. A round-1 belief has
round 0 and an empty hash, so older cache files (without the escalation fields) still load as round-1 beliefs.
An escalation belief (WP A's ``EscalationContext``, DECISIONS #28/#39) is keyed by its round and by
exactly what the trader saw: its own previous estimate and the other traders' rationales and risks. A
different debate is a different question. The price is not in the key, so re-running the league with other
market parameters still hits the cache whenever the debate was the same.

The JSONL file is the offline demo fallback and the league's belief cache format. Each line is a
``BeliefRecord``. Invalid lines are skipped (and counted); a miss abstains, or asks the fallback provider.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
from collections.abc import Iterable, Mapping
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from prm.config import TraderSpec
from prm.traders.base import BeliefProvider, abstain
from prm.traders.context import visible_peers
from prm.types import BeliefResult, EscalationContext, PRBundle, TraderContext

Key = tuple[str, str, int, str]


class BeliefRecord(BaseModel):
    model_config = ConfigDict(extra="ignore")

    trader: str
    diff_sha256: str
    escalation_round: int = 0
    escalation_sha256: str = ""
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


def escalation_key(escalation: EscalationContext, trader: str) -> str:
    """sha256 of exactly what this trader is shown: its previous estimate and the peers (not itself)."""
    payload = {
        "previous": [escalation.previous_p_yes, escalation.previous_rationale],
        "peers": [[p.trader, p.rationale, list(p.key_risks)] for p in visible_peers(escalation, trader)],
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False).encode("utf-8")).hexdigest()


def belief_key(trader: str, diff: str, escalation: EscalationContext | None = None) -> Key:
    if escalation is None:
        return (trader, diff_key(diff), 0, "")
    return (trader, diff_key(diff), escalation.round, escalation_key(escalation, trader))


def to_record(result: BeliefResult, diff: str, escalation: EscalationContext | None = None) -> BeliefRecord:
    _, diff_sha, rnd, shown_sha = belief_key(result.trader, diff, escalation)
    return BeliefRecord(
        diff_sha256=diff_sha, escalation_round=rnd, escalation_sha256=shown_sha, **dataclasses.asdict(result)
    )


def to_result(rec: BeliefRecord) -> BeliefResult:
    return BeliefResult(**rec.model_dump(exclude={"diff_sha256", "escalation_round", "escalation_sha256"}))


def append_beliefs(
    path: Path,
    diff: str,
    results: Iterable[BeliefResult],
    escalations: Mapping[str, EscalationContext] | None = None,
) -> None:
    """Append beliefs for one diff. ``escalations`` maps trader -> the EscalationContext it saw (if any)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        for r in results:
            esc = (escalations or {}).get(r.trader)
            fh.write(to_record(r, diff, esc).model_dump_json() + "\n")


def load_beliefs(path: Path) -> tuple[dict[Key, BeliefResult], int]:
    """``{belief_key: belief}`` (last line wins) and the number of invalid lines skipped."""
    table: dict[Key, BeliefResult] = {}
    skipped = 0
    with path.open(encoding="utf-8") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                rec = BeliefRecord.model_validate(json.loads(line))
                key = (rec.trader, rec.diff_sha256, rec.escalation_round, rec.escalation_sha256)
                table[key] = to_result(rec)
            except (ValueError, ValidationError):
                skipped += 1
    return table, skipped


class ReplayProvider:
    def __init__(self, path: Path, fallback: BeliefProvider | None = None) -> None:
        self.path = Path(path)
        self.fallback = fallback
        self._table: dict[Key, BeliefResult] | None = None
        self.load_error: str | None = None
        self.skipped = 0

    def _lookup(self, key: Key) -> BeliefResult | None:
        if self._table is None:
            try:
                self._table, self.skipped = load_beliefs(self.path)
            except OSError as exc:
                self._table, self.load_error = {}, f"cannot read replay file: {type(exc).__name__}"
        return self._table.get(key)

    async def belief(
        self,
        spec: TraderSpec,
        ctx: TraderContext,
        bundle: PRBundle,
        escalation: EscalationContext | None = None,
    ) -> BeliefResult:
        hit = self._lookup(belief_key(spec.name, bundle.diff, escalation))
        if hit is not None:
            return hit
        if self.fallback is not None:
            if escalation is None:
                return await self.fallback.belief(spec, ctx, bundle)
            return await self.fallback.belief(spec, ctx, bundle, escalation=escalation)
        return abstain(spec.name, "replay", self.load_error or "no replayed belief for this trader and diff")
