import math
from pathlib import Path

import pytest

from prm.config import (
    ConfigError,
    RuntimeEnv,
    ServerConfig,
    TargetGateOverrides,
    load_server_config,
    load_target_config,
    parse_server_config,
    parse_target_config,
    placeholders_in,
    render_command,
    render_env,
)
from prm.settle.postmerge import DEFAULT_WINDOW_DAYS
from prm.types import TRADER_ROLES

ROOT = Path(__file__).resolve().parents[1]

# PLAN §3.1, verbatim.
PLAN_EXAMPLE = """
[target]
name = "sample-target"
holdouts_repo = "owner/sample-target-holdouts"
spec = "docs/SPEC.md"

[settle]
driver = "local"
setup = ""
command = "python -m pytest {holdouts} -q -p no:cacheprovider --junitxml={junit}"
timeout_s = 180
env = { PYTHONPATH = "{head}" }

[redteam]
command = ""

[context]
test_globs = ["tests/**", "**/test_*.py", "**/*_test.py", "**/*.test.ts", "**/*.spec.ts"]
sensitive_globs = [
  "**/auth*", "**/security*", "**/permissions*", "**/middleware*",
  ".github/**", "**/migrations/**", "**/*.env*",
]
source_globs = ["**/*.py", "**/*.ts", "**/*.js"]

[gate]
auto_merge = 0.90
human_review = 0.60
"""

MINIMAL = """
[target]
name = "t"
holdouts_repo = "o/r"
[settle]
command = "pytest {holdouts} --junitxml={junit}"
"""


def target(extra: str = "", base: str = MINIMAL, **kw):
    return parse_target_config(base + extra, **kw)


# ------------------------------------------------------------------------------------------------ server


def test_shipped_server_config_loads():
    cfg = load_server_config(ROOT / "prm.server.toml")
    assert tuple(t.name for t in cfg.traders) == TRADER_ROLES
    assert cfg.trader("security").model == "claude-sonnet-5-5"
    assert cfg.market.b == 20.0 and cfg.gate.auto_merge == 0.90
    assert cfg.evolution.enabled is False
    assert cfg.market.max_escalations == 2  # DECISIONS #28


def test_defaults_match_shipped_file():
    assert ServerConfig() == load_server_config(ROOT / "prm.server.toml")


def test_load_server_config_honours_env(monkeypatch, tmp_path):
    p = tmp_path / "s.toml"
    p.write_text("[market]\nb = 25.0\n")
    monkeypatch.setenv("PRM_SERVER_CONFIG", str(p))
    assert load_server_config().market.b == 25.0


def test_solo_auto_merge_costs_more_than_cap():
    # 0.50 -> 0.90 costs b*ln5 ~= 32.19 > 30 (R-MKT-3).
    assert math.isclose(20 * math.log(5), 32.188758248682, rel_tol=1e-12)
    ServerConfig()  # the default config is safe


@pytest.mark.parametrize(
    "toml",
    [
        "[market]\nmax_spend_abs = 33.0\n",  # one trader could buy 0.5 -> 0.9
        "[gate]\nauto_merge = 0.8\n",  # 0.5 -> 0.8 costs ~18.3
        "[market]\ninitial_price = 0.95\n",  # opens above auto-merge
        "[gate]\nhuman_review = 0.95\n",  # review above auto-merge
        "[market]\nmin_traders = 1\n",  # fail closed needs >= 2
        "[market]\nbogus = 1\n",  # unknown key
        "bogus = 1\n",  # unknown top-level key
        '[[traders]]\nname = "oracle"\nmodel = "claude-haiku-5-5"\n',  # unknown role
        # unpriced model
        '[[traders]]\nname = "security"\nmodel = "x"\n[[traders]]\nname = "skeptic"\nmodel = "x"\n',
        '[[traders]]\nname = "skeptic"\nmodel = "claude-haiku-5-5"\n'
        '[[traders]]\nname = "skeptic"\nmodel = "claude-haiku-5-5"\n',  # duplicate
        '[[traders]]\nname = "skeptic"\nmodel = "claude-haiku-5-5"\n',  # 1 trader < min_traders 2
        '[pricing_usd_per_mtok]\n"claude-haiku-5-5" = [-1, 0]\n',
        "[market\n",  # bad TOML
    ],
)
def test_invalid_server_configs_raise(toml):
    with pytest.raises(ConfigError):
        parse_server_config(toml)


def test_gate_for_merges_overrides_and_rejects_unsafe():
    cfg = ServerConfig()
    assert cfg.gate_for(None) == cfg.gate
    g = cfg.gate_for(TargetGateOverrides(auto_merge=0.95))
    assert (g.auto_merge, g.human_review) == (0.95, 0.60)
    with pytest.raises(ConfigError, match="no-solo-auto-merge"):
        cfg.gate_for(TargetGateOverrides(auto_merge=0.85))
    with pytest.raises(ConfigError):
        cfg.gate_for(TargetGateOverrides(human_review=0.92))


def test_cost_usd():
    cfg = ServerConfig()
    assert math.isclose(cfg.cost_usd("claude-haiku-5-5", 10_000, 500), (10_000 * 0.10 + 500 * 0.50) / 1e6)
    with pytest.raises(KeyError):
        cfg.cost_usd("unknown-model", 1, 1)


def test_config_is_frozen():
    cfg = ServerConfig()
    with pytest.raises(ValueError):
        cfg.market.b = 1.0  # type: ignore[misc]


# ------------------------------------------------------------------------------------------------ target


def test_plan_example_parses():
    cfg = parse_target_config(PLAN_EXAMPLE)
    assert cfg.target.holdouts_repo == "owner/sample-target-holdouts"
    assert cfg.settle.env == {"PYTHONPATH": "{head}"}
    assert cfg.settle.setup_commands == []
    assert not cfg.redteam.enabled
    assert cfg.gate.auto_merge == 0.90
    assert "**/auth*" in cfg.context.sensitive_globs


def test_minimal_target_gets_defaults():
    cfg = target()
    assert cfg.settle.driver == "local" and cfg.settle.timeout_s == 180
    assert "tests/**" in cfg.context.test_globs
    assert cfg.gate.auto_merge is None
    assert cfg.holdouts_dir(Path("/x")) is None
    assert cfg.postmerge.window_days == DEFAULT_WINDOW_DAYS == 14


def test_postmerge_window_is_configurable():
    assert target("[postmerge]\nwindow_days = 30\n").postmerge.window_days == 30


@pytest.mark.parametrize(
    "toml",
    [
        MINIMAL + "surprise = 1\n",  # unknown key in [settle]
        MINIMAL + "[extra]\na = 1\n",  # unknown section
        MINIMAL.replace('holdouts_repo = "o/r"', ""),  # no holdouts source
        MINIMAL.replace('holdouts_repo = "o/r"', 'holdouts_repo = "o/r"\nholdouts_path = "../h"'),  # both
        MINIMAL.replace('"o/r"', '"not-a-repo"'),
        MINIMAL.replace('"o/r"', '"o/.."'),
        MINIMAL.replace('name = "t"', 'name = "../evil"'),
        MINIMAL.replace("{junit}", "{secret}"),  # unknown placeholder
        MINIMAL.replace("{junit}", "{0}"),  # positional
        MINIMAL.replace("{junit}", "{junit!r}"),  # conversion
        MINIMAL.replace("{junit}", "{junit.x}"),  # attribute access
        MINIMAL.replace('"pytest {holdouts} --junitxml={junit}"', '"  "'),  # empty command
        MINIMAL.replace('"pytest {holdouts} --junitxml={junit}"', '"pytest \'unbalanced"'),
        MINIMAL + 'driver = "docker"\n',
        MINIMAL + "timeout_s = 0\n",
        MINIMAL + 'env = { HOME = "/tmp" }\n',  # reserved
        MINIMAL + 'env = { "BAD-NAME" = "x" }\n',
        MINIMAL + 'env = { X = "{nope}" }\n',
        MINIMAL + 'setup = ["pip install {nope}"]\n',
        MINIMAL + '[redteam]\ncommand = "rt --out x.json"\n',  # must write to {findings}
        MINIMAL + "[gate]\nauto_merge = 1.5\n",
        MINIMAL + "[postmerge]\nwindow_days = 0\n",
        MINIMAL + "[postmerge]\nwindow_days = 365\n",
        MINIMAL + '[postmerge]\nfix_pattern = ".*"\n',  # not configurable: an untrusted regex
        MINIMAL + '[context]\ntest_globs = ["/abs/**"]\n',
        MINIMAL.replace('name = "t"', 'name = "t"\nspec = "../../etc/passwd"'),
        MINIMAL.replace('name = "t"', 'name = "t"\nspec = "/etc/passwd"'),
        "[target\n",
    ],
)
def test_invalid_target_configs_raise(toml):
    with pytest.raises(ConfigError):
        parse_target_config(toml, allow_holdouts_path=True)


def test_setup_list_and_redteam():
    cfg = target(
        'setup = ["pip install -e .", "  ", "npm ci"]\n[redteam]\ncommand = "rt {head} --out {findings}"\n'
    )
    assert cfg.settle.setup_commands == ["pip install -e .", "npm ci"]
    assert cfg.redteam.enabled


def test_holdouts_path_only_for_local_configs(tmp_path):
    text = MINIMAL.replace('holdouts_repo = "o/r"', 'holdouts_path = "../holdouts"')
    with pytest.raises(ConfigError, match="only allowed for local targets"):
        parse_target_config(text)  # e.g. a .prm.toml fetched from GitHub
    root = tmp_path / "target"
    root.mkdir()
    (root / ".prm.toml").write_text(text)
    cfg = load_target_config(root)  # a directory or the file both work
    assert cfg == load_target_config(root / ".prm.toml")
    assert cfg.holdouts_dir(root) == (tmp_path / "holdouts").resolve()


def test_load_target_config_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="cannot read"):
        load_target_config(tmp_path)


def test_oversized_target_config_rejected():
    with pytest.raises(ConfigError, match="larger than"):
        parse_target_config(MINIMAL + "#" * 70_000)


# ---------------------------------------------------------------------------------------- placeholders


def test_placeholders_in():
    assert placeholders_in("pytest {holdouts} --junitxml={junit} {{literal}}") == {"holdouts", "junit"}
    assert placeholders_in("no placeholders") == set()


def test_render_command_splits_before_filling():
    argv = render_command(
        "python -m pytest {holdouts} -q --junitxml={junit}",
        {"holdouts": "/tmp/my holdouts", "junit": Path("/tmp/out dir/j.xml")},
    )
    assert argv == ["python", "-m", "pytest", "/tmp/my holdouts", "-q", "--junitxml=/tmp/out dir/j.xml"]


def test_render_command_does_not_reinterpret_values():
    # A value with shell metacharacters or braces stays one literal token.
    argv = render_command("echo {head}", {"head": "; rm -rf / {junit}"})
    assert argv == ["echo", "; rm -rf / {junit}"]


def test_render_command_missing_value():
    with pytest.raises(ConfigError):
        render_command("pytest {holdouts}", {})


def test_render_env():
    assert render_env({"PYTHONPATH": "{head}:{head}/src"}, {"head": "/w"}) == {"PYTHONPATH": "/w:/w/src"}


# ----------------------------------------------------------------------------------------- runtime env


def test_runtime_env_defaults():
    env = RuntimeEnv.from_env({})
    assert env.database_url == "sqlite:///prm.db"
    assert env.fake_llm is False and env.sandbox == "subprocess" and env.replay_beliefs is None


def test_runtime_env_from_values():
    env = RuntimeEnv.from_env(
        {
            "PRM_DB": "/data/prm.db",
            "PRM_CACHE": "/data/cache",
            "PRM_PUBLIC_URL": "https://prm.example/",
            "PRM_FAKE_LLM": "1",
            "PRM_REPLAY_BELIEFS": "b.jsonl",
            "PRM_SANDBOX": "docker",
        }
    )
    assert env.database_url == "sqlite:////data/prm.db"
    assert env.cache_dir == Path("/data/cache")
    assert env.public_url == "https://prm.example"
    assert env.fake_llm and env.sandbox == "docker" and env.replay_beliefs == Path("b.jsonl")
    assert RuntimeEnv.from_env({"DATABASE_URL": "postgresql://x"}).database_url == "postgresql://x"


def test_runtime_env_rejects_bad_sandbox():
    with pytest.raises(ConfigError):
        RuntimeEnv.from_env({"PRM_SANDBOX": "chroot"})


@pytest.mark.parametrize("value", [-1, 6])
def test_max_escalations_bounds(value):
    with pytest.raises(ConfigError):
        parse_server_config(f"[market]\nmax_escalations = {value}\n")
    assert parse_server_config("[market]\nmax_escalations = 0\n").market.max_escalations == 0
