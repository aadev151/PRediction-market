"""Configuration models and loaders (PLAN §3).

- ``ServerConfig``: the factory's own config (``prm.server.toml``).
- ``TargetConfig``: a target repo's contract (``.prm.toml``). In production it is read from the PR's BASE sha,
  never the head, so a PR can't rewrite its own gate (R-TGT-3).
- ``RuntimeEnv``: non-secret runtime settings from environment variables.
  Secrets are read where they are used, never here.

Every model forbids unknown keys. Commands are split with ``shlex`` and then placeholders are filled per
token, so a path containing spaces stays one argument and nothing ever goes through a shell.
"""

from __future__ import annotations

import math
import os
import re
import shlex
import string
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

from prm.types import TraderRole

TARGET_CONFIG_FILENAME = ".prm.toml"
DEFAULT_SERVER_CONFIG = "prm.server.toml"
MAX_TARGET_CONFIG_BYTES = 64 * 1024

SETTLE_PLACEHOLDERS = frozenset({"head", "holdouts", "junit", "tmp"})
REDTEAM_PLACEHOLDERS = SETTLE_PLACEHOLDERS | {"findings"}
# The sandbox owns these; a target may not override them.
RESERVED_ENV = frozenset({"HOME", "TMPDIR"})

_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_REPO_NAME = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class ConfigError(ValueError):
    """A config file is missing, unparsable or invalid. Callers fail closed on it."""


# --------------------------------------------------------------------------------------------- placeholders


def placeholders_in(template: str) -> set[str]:
    """Names of ``{placeholders}`` in ``template``. Rejects positional/attribute/index/format-spec forms."""
    names: set[str] = set()
    try:
        parsed = list(string.Formatter().parse(template))
    except ValueError as exc:
        raise ValueError(f"bad placeholder syntax in {template!r}: {exc}") from exc
    for _literal, field, spec, conversion in parsed:
        if field is None:
            continue
        if not field.isidentifier() or spec or conversion:
            raise ValueError(f"only plain {{name}} placeholders are allowed, got {{{field}}} in {template!r}")
        names.add(field)
    return names


def _check_placeholders(template: str, allowed: frozenset[str], what: str) -> None:
    unknown = placeholders_in(template) - allowed
    if unknown:
        raise ValueError(f"{what}: unknown placeholder(s) {sorted(unknown)}; allowed: {sorted(allowed)}")


def _fill(token: str, values: Mapping[str, str | os.PathLike[str]]) -> str:
    try:
        return string.Formatter().vformat(token, (), {k: os.fspath(v) for k, v in values.items()})
    except KeyError as exc:
        raise ConfigError(f"no value for placeholder {exc} in {token!r}") from exc


def render_command(template: str, values: Mapping[str, str | os.PathLike[str]]) -> list[str]:
    """``shlex.split`` the template, then fill placeholders per token. The result goes to ``shell=False``."""
    return [_fill(tok, values) for tok in shlex.split(template)]


def render_env(env: Mapping[str, str], values: Mapping[str, str | os.PathLike[str]]) -> dict[str, str]:
    return {k: _fill(v, values) for k, v in env.items()}


def _safe_relpath(value: str, what: str) -> str:
    p = PurePosixPath(value)
    if not value or p.is_absolute() or ".." in p.parts or "\\" in value:
        raise ValueError(f"{what} must be a relative path inside the repo, got {value!r}")
    return value


def solo_yes_cost(p_from: float, p_to: float, b: float) -> float:
    """LMSR cost of buying YES from ``p_from`` to ``p_to`` (PLAN §6.3: b*ln((1-p)/(1-p2)))."""
    return b * math.log((1.0 - p_from) / (1.0 - p_to))


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------------------------------------- server


class MarketParams(_Strict):
    b: float = Field(20.0, gt=0)
    initial_price: float = Field(0.5, gt=0, lt=1)
    rounds: int = Field(3, ge=1, le=10)
    start_bankroll: float = Field(100.0, gt=0)
    max_spend_fraction: float = Field(0.25, gt=0, le=1)
    max_spend_abs: float = Field(30.0, gt=0)
    min_traders: int = Field(2, ge=2)  # fewer than 2 responding traders always VOIDs (R-SM-2)
    # Escalation rounds while the price sits in [human_review, auto_merge) (DECISIONS #28). 0 disables them.
    max_escalations: int = Field(2, ge=0, le=5)


class GateThresholds(_Strict):
    """``auto_merge`` = A: merge at or above. ``human_review`` = B, the reject line: below it the PR is
    blocked; between B and A the market escalates, then fails "no consensus" (DECISIONS #28, name kept)."""

    auto_merge: float = Field(0.90, gt=0, lt=1)
    human_review: float = Field(0.60, gt=0, lt=1)

    @model_validator(mode="after")
    def _ordered(self) -> GateThresholds:
        if not self.human_review < self.auto_merge:
            raise ValueError("gate: human_review must be < auto_merge")
        return self


class TargetGateOverrides(_Strict):
    auto_merge: float | None = Field(None, gt=0, lt=1)
    human_review: float | None = Field(None, gt=0, lt=1)


class TraderSpec(_Strict):
    name: TraderRole
    model: str = Field(min_length=1)


class LeagueParams(_Strict):
    n: int = Field(120, ge=1)
    bug_fraction: float = Field(0.4, ge=0, le=1)
    concurrency: int = Field(8, ge=1, le=64)
    warmup: int = Field(40, ge=0)
    seed: int = 7


class EvolutionParams(_Strict):
    """P2 retire-and-respawn (PLAN §6.16). Off by default."""

    enabled: bool = False
    retire_below: float = Field(20.0, ge=0)
    max_generations: int = Field(3, ge=1)


def _default_roster() -> list[TraderSpec]:
    return [
        TraderSpec(name="diff_reader", model="claude-haiku-5-5"),
        TraderSpec(name="coverage", model="claude-haiku-5-5"),
        TraderSpec(name="security", model="claude-sonnet-5-5"),
        TraderSpec(name="skeptic", model="claude-haiku-5-5"),
    ]


def _default_pricing() -> dict[str, tuple[float, float]]:
    return {
        "claude-haiku-5-5": (0.10, 0.50),
        "claude-sonnet-5-5": (2.00, 10.00),
        "claude-opus-5-5": (4.00, 20.00),
    }


class ServerConfig(_Strict):
    market: MarketParams = MarketParams()
    gate: GateThresholds = GateThresholds()
    traders: list[TraderSpec] = Field(default_factory=_default_roster, min_length=1)
    pricing_usd_per_mtok: dict[str, tuple[float, float]] = Field(default_factory=_default_pricing)
    league: LeagueParams = LeagueParams()
    evolution: EvolutionParams = EvolutionParams()

    @field_validator("pricing_usd_per_mtok")
    @classmethod
    def _nonnegative_prices(cls, v: dict[str, tuple[float, float]]) -> dict[str, tuple[float, float]]:
        for model, (inp, out) in v.items():
            if inp < 0 or out < 0 or not (math.isfinite(inp) and math.isfinite(out)):
                raise ValueError(f"pricing for {model!r} must be finite and non-negative")
        return v

    @model_validator(mode="after")
    def _consistent(self) -> ServerConfig:
        names = [t.name for t in self.traders]
        if len(set(names)) != len(names):
            raise ValueError(f"duplicate trader names in roster: {names}")
        unpriced = sorted({t.model for t in self.traders} - set(self.pricing_usd_per_mtok))
        if unpriced:
            raise ValueError(f"no pricing for model(s) {unpriced}")
        if self.market.min_traders > len(self.traders):
            raise ValueError("market.min_traders exceeds the number of configured traders")
        self.check_no_solo_auto_merge(self.gate)
        return self

    def check_no_solo_auto_merge(self, gate: GateThresholds) -> None:
        """R-MKT-3: one trader's max spend must not move the price from the start to auto-merge."""
        m = self.market
        if gate.auto_merge <= m.initial_price:
            raise ValueError(f"unsafe gate: markets would open at or above auto_merge {gate.auto_merge}")
        cost = solo_yes_cost(m.initial_price, gate.auto_merge, m.b)
        if cost <= m.max_spend_abs:
            raise ValueError(
                f"unsafe gate: one trader can move {m.initial_price} -> {gate.auto_merge} for {cost:.2f} "
                f"<= max_spend_abs {m.max_spend_abs} (no-solo-auto-merge invariant)"
            )

    def gate_for(self, overrides: TargetGateOverrides | None) -> GateThresholds:
        """Server gate merged with a target's overrides. Raises ConfigError if the result is unsafe."""
        merged = self.gate.model_dump()
        if overrides is not None:
            merged.update(overrides.model_dump(exclude_none=True))
        try:
            gate = GateThresholds(**merged)
            self.check_no_solo_auto_merge(gate)
        except (ValidationError, ValueError) as exc:
            raise ConfigError(f"invalid gate overrides: {exc}") from exc
        return gate

    def trader(self, name: str) -> TraderSpec:
        for t in self.traders:
            if t.name == name:
                return t
        raise KeyError(name)

    def cost_usd(self, model: str, input_tokens: int, output_tokens: int) -> float:
        inp, out = self.pricing_usd_per_mtok[model]
        return (input_tokens * inp + output_tokens * out) / 1_000_000


# ---------------------------------------------------------------------------------------------- target


class TargetSection(_Strict):
    name: str = Field(pattern=r"^[A-Za-z0-9._-]{1,100}$")
    holdouts_repo: str | None = None
    holdouts_path: str | None = None
    spec: str | None = None

    @field_validator("holdouts_repo")
    @classmethod
    def _repo(cls, v: str | None) -> str | None:
        if v is not None and (not _REPO_NAME.match(v) or any(s in {".", ".."} for s in v.split("/"))):
            raise ValueError(f"holdouts_repo must look like 'owner/repo', got {v!r}")
        return v

    @field_validator("spec")
    @classmethod
    def _spec(cls, v: str | None) -> str | None:
        return None if v is None else _safe_relpath(v, "target.spec")

    @model_validator(mode="after")
    def _one_holdouts_source(self) -> TargetSection:
        # At most one source. None is fine: settlement is the post-merge oracle (DECISIONS #57), so a GitHub
        # target needs no hidden-test repo; only the league's local oracle reads ``holdouts_path``.
        if self.holdouts_repo is not None and self.holdouts_path is not None:
            raise ValueError("set at most one of target.holdouts_repo or target.holdouts_path")
        return self


class SettleSection(_Strict):
    driver: Literal["local", "remote"] = "local"
    setup: str | list[str] = ""
    command: str = Field(min_length=1)
    timeout_s: int = Field(180, ge=1, le=3600)
    env: dict[str, str] = Field(default_factory=dict)

    @field_validator("command")
    @classmethod
    def _command(cls, v: str) -> str:
        if not shlex.split(v):
            raise ValueError("settle.command is empty")
        _check_placeholders(v, SETTLE_PLACEHOLDERS, "settle.command")
        return v

    @field_validator("setup")
    @classmethod
    def _setup(cls, v: str | list[str]) -> str | list[str]:
        for cmd in [v] if isinstance(v, str) else v:
            shlex.split(cmd)  # unbalanced quotes raise ValueError
            _check_placeholders(cmd, SETTLE_PLACEHOLDERS, "settle.setup")
        return v

    @field_validator("env")
    @classmethod
    def _env(cls, v: dict[str, str]) -> dict[str, str]:
        for key, value in v.items():
            if not _ENV_NAME.match(key):
                raise ValueError(f"settle.env: invalid variable name {key!r}")
            if key in RESERVED_ENV:
                raise ValueError(f"settle.env: {key} is owned by the sandbox")
            _check_placeholders(value, SETTLE_PLACEHOLDERS, f"settle.env.{key}")
        return v

    @property
    def setup_commands(self) -> list[str]:
        cmds = [self.setup] if isinstance(self.setup, str) else list(self.setup)
        return [c for c in cmds if c.strip()]


class RedteamSection(_Strict):
    command: str = ""

    @field_validator("command")
    @classmethod
    def _command(cls, v: str) -> str:
        if v.strip():
            shlex.split(v)
            _check_placeholders(v, REDTEAM_PLACEHOLDERS, "redteam.command")
            if "findings" not in placeholders_in(v):
                raise ValueError("redteam.command must write its findings JSON to {findings}")
        return v

    @property
    def enabled(self) -> bool:
        return bool(self.command.strip())


class PostmergeSection(_Strict):
    """Post-merge oracle (DECISIONS #26, #57): how long a merged PR must go without a revert or fix."""

    window_days: int = Field(14, ge=1, le=90)


class ContextSection(_Strict):
    test_globs: list[str] = Field(
        default_factory=lambda: ["tests/**", "**/test_*.py", "**/*_test.py", "**/*.test.ts", "**/*.spec.ts"]
    )
    sensitive_globs: list[str] = Field(
        default_factory=lambda: [
            "**/auth*",
            "**/security*",
            "**/permissions*",
            "**/middleware*",
            ".github/**",
            "**/migrations/**",
            "**/*.env*",
        ]
    )
    source_globs: list[str] = Field(default_factory=lambda: ["**/*.py", "**/*.ts", "**/*.js"])

    @field_validator("test_globs", "sensitive_globs", "source_globs")
    @classmethod
    def _relative_globs(cls, v: list[str]) -> list[str]:
        for g in v:
            if not g.strip() or g.startswith("/"):
                raise ValueError(f"globs must be non-empty and relative, got {g!r}")
        return v


class TargetConfig(_Strict):
    target: TargetSection
    settle: SettleSection
    redteam: RedteamSection = RedteamSection()
    postmerge: PostmergeSection = PostmergeSection()
    context: ContextSection = ContextSection()
    gate: TargetGateOverrides = TargetGateOverrides()

    def holdouts_dir(self, target_root: Path) -> Path | None:
        """Local holdouts directory (``holdouts_path`` is relative to the target root). None for a repo."""
        if self.target.holdouts_path is None:
            return None
        return (Path(target_root) / self.target.holdouts_path).resolve()


# --------------------------------------------------------------------------------------------- loaders


def _parse_toml(text: str, source: str) -> dict:
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{source}: invalid TOML: {exc}") from exc


def parse_server_config(text: str, source: str = "<server config>") -> ServerConfig:
    try:
        return ServerConfig.model_validate(_parse_toml(text, source))
    except ValidationError as exc:
        raise ConfigError(f"{source}: {exc}") from exc


def load_server_config(path: str | os.PathLike[str] | None = None) -> ServerConfig:
    p = Path(path or os.environ.get("PRM_SERVER_CONFIG", DEFAULT_SERVER_CONFIG))
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read server config {p}: {exc}") from exc
    return parse_server_config(text, str(p))


def parse_target_config(
    text: str, *, source: str = TARGET_CONFIG_FILENAME, allow_holdouts_path: bool = False
) -> TargetConfig:
    """Parse a ``.prm.toml``.

    ``allow_holdouts_path`` must stay False for configs fetched from GitHub: a local path in a remote config
    would let a repo point the factory at arbitrary files on the server.
    """
    if len(text.encode("utf-8")) > MAX_TARGET_CONFIG_BYTES:
        raise ConfigError(f"{source}: larger than {MAX_TARGET_CONFIG_BYTES} bytes")
    try:
        cfg = TargetConfig.model_validate(_parse_toml(text, source))
    except ValidationError as exc:
        raise ConfigError(f"{source}: {exc}") from exc
    if cfg.target.holdouts_path is not None and not allow_holdouts_path:
        raise ConfigError(
            f"{source}: target.holdouts_path is only allowed for local targets; use holdouts_repo"
        )
    return cfg


def load_target_config(path: str | os.PathLike[str]) -> TargetConfig:
    """Load a local target's config. ``path`` is the target root or the ``.prm.toml`` file itself."""
    p = Path(path)
    if p.is_dir():
        p = p / TARGET_CONFIG_FILENAME
    try:
        text = p.read_text(encoding="utf-8")
    except OSError as exc:
        raise ConfigError(f"cannot read target config {p}: {exc}") from exc
    return parse_target_config(text, source=str(p), allow_holdouts_path=True)


# ------------------------------------------------------------------------------------------- runtime env


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class RuntimeEnv:
    """Non-secret settings from the environment (PLAN §8.6). Secrets are read at their point of use."""

    db_path: str = "prm.db"
    database_url_override: str | None = None
    cache_dir: Path = Path(".prm-cache")
    public_url: str = "http://localhost:8000"
    fake_llm: bool = False
    replay_beliefs: Path | None = None
    sandbox: Literal["subprocess", "docker"] = "subprocess"
    server_config: Path = Path(DEFAULT_SERVER_CONFIG)

    @property
    def database_url(self) -> str:
        return self.database_url_override or f"sqlite:///{self.db_path}"

    @classmethod
    def from_env(cls, environ: Mapping[str, str] | None = None) -> RuntimeEnv:
        env = os.environ if environ is None else environ
        sandbox = env.get("PRM_SANDBOX", "subprocess") or "subprocess"
        if sandbox not in ("subprocess", "docker"):
            raise ConfigError(f"PRM_SANDBOX must be 'subprocess' or 'docker', got {sandbox!r}")
        replay = env.get("PRM_REPLAY_BELIEFS") or None
        return cls(
            db_path=env.get("PRM_DB") or "prm.db",
            database_url_override=env.get("DATABASE_URL") or None,
            cache_dir=Path(env.get("PRM_CACHE") or ".prm-cache"),
            public_url=(env.get("PRM_PUBLIC_URL") or "http://localhost:8000").rstrip("/"),
            fake_llm=_truthy(env.get("PRM_FAKE_LLM")),
            replay_beliefs=Path(replay) if replay else None,
            sandbox=sandbox,  # type: ignore[arg-type]
            server_config=Path(env.get("PRM_SERVER_CONFIG") or DEFAULT_SERVER_CONFIG),
        )
