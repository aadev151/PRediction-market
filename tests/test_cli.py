import os
from pathlib import Path

import pytest

from prm.cli import DOTENV_FILE, load_local_env, main

ROOT = Path(__file__).resolve().parents[1]


def test_validate_shipped_config(capsys):
    assert main(["validate", "--server", str(ROOT / "prm.server.toml")]) == 0
    assert "server config OK" in capsys.readouterr().out


def test_validate_target(tmp_path, capsys):
    (tmp_path / ".prm.toml").write_text(
        '[target]\nname="t"\nholdouts_path="../h"\n[settle]\ncommand="pytest"\n'
    )
    assert main(["validate", "--server", str(ROOT / "prm.server.toml"), "--target", str(tmp_path)]) == 0
    assert "target config OK: t" in capsys.readouterr().out


def test_validate_reports_errors(tmp_path, capsys):
    bad = tmp_path / "s.toml"
    bad.write_text("[market]\nmax_spend_abs = 50.0\n")
    assert main(["validate", "--server", str(bad)]) == 1
    assert "no-solo-auto-merge" in capsys.readouterr().err


def test_version(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert "prm 0.1.0" in capsys.readouterr().out


def test_smoke_llm_without_key(monkeypatch, capsys):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert main(["smoke-llm", "--server", str(ROOT / "prm.server.toml")]) == 2
    assert "ANTHROPIC_API_KEY is not set" in capsys.readouterr().err


def test_smoke_llm_prints_one_line_per_model(monkeypatch, capsys):
    from types import SimpleNamespace

    from prm.traders import llm

    class _Messages:
        async def parse(self, **kwargs):
            belief = llm.Belief(p_yes=0.3, rationale="subtraction bug", key_risks=[])
            usage = SimpleNamespace(input_tokens=10, output_tokens=5)
            return SimpleNamespace(parsed_output=belief, usage=usage, stop_reason="end_turn")

    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-not-a-real-key")
    monkeypatch.setattr(llm, "make_client", lambda: SimpleNamespace(messages=_Messages()))
    assert main(["smoke-llm", "--server", str(ROOT / "prm.server.toml")]) == 0
    out = capsys.readouterr().out
    assert "claude-haiku-5-5: p_yes=0.30" in out and "claude-sonnet-5-5: p_yes=0.30" in out


# ---------------------------------------------------------------------------------------------- WP E


def _deps(tmp_path):
    import random

    from prm.config import RuntimeEnv, ServerConfig
    from prm.db import make_session_factory
    from prm.market.engine import MarketEngine
    from prm.pipeline import PipelineDeps
    from prm.traders.fake import FakeProvider
    from prm.traders.roster import EngineProvider

    cfg = ServerConfig()
    sf = make_session_factory("sqlite://")
    engine = MarketEngine(sf, cfg, EngineProvider(FakeProvider()), rng=random.Random(0))  # noqa: S311
    env = RuntimeEnv(cache_dir=tmp_path / "cache", public_url="http://prm.test")
    return PipelineDeps(env, cfg, sf, engine, None)


def test_parse_market_ref():
    from prm.cli import build_parser

    assert build_parser().parse_args(["market", "acme/widgets#12"]).pr == ("acme/widgets", 12)


@pytest.mark.parametrize("argv", [["market", "acme/widgets"], ["market", "acme#1"], ["repo", "add", "../x"]])
def test_bad_refs_rejected(argv):
    with pytest.raises(SystemExit) as exc:
        main(argv)
    assert exc.value.code == 2


def test_serve_runs_uvicorn_factory(monkeypatch):
    import uvicorn

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, **kw: calls.append((app, kw)))
    assert main(["serve", "--host", "127.0.0.1", "--port", "9001"]) == 0
    assert calls == [
        ("prm.server:create_app", {"factory": True, "host": "127.0.0.1", "port": 9001, "reload": False})
    ]


def test_repo_add(monkeypatch, tmp_path, capsys):
    from prm import pipeline

    monkeypatch.setattr(pipeline, "_default", _deps(tmp_path))
    assert main(["repo", "add", "acme/widgets"]) == 0
    assert "repo acme/widgets registered" in capsys.readouterr().out


def test_settle_is_post_merge_now(capsys):
    assert main(["settle", "--target", str(ROOT / "examples" / "sample-target")]) == 2
    assert "settlement is post-merge now" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("kind", "patch", "expected"),
    [
        ("bug", "bug_auth.patch", "gate failure · outcome NO"),
        ("good", "good_docstring.patch", "gate success · outcome YES"),
    ],
)
def test_demo_local_settles_post_merge(monkeypatch, tmp_path, capsys, kind, patch, expected):
    from prm import pipeline

    monkeypatch.setattr(pipeline, "_default", _deps(tmp_path))
    argv = [
        "demo",
        kind,
        "--local",
        "--target",
        str(ROOT / "examples" / "sample-target"),
        "--repo",
        "local/s",
    ]
    assert main(argv) == 0
    out = capsys.readouterr().out
    assert f"demo {kind}: {patch} on local/s" in out
    assert "market 1: SETTLED" in out and expected in out and "· market right" in out
    assert "http://prm.test/m/1" in out


def test_demo_needs_local(capsys):
    assert main(["demo", "good", "--target", "x", "--repo", "a/b"]) == 2
    assert "not implemented" in capsys.readouterr().err


def test_demo_missing_patch(tmp_path, capsys):
    assert main(["demo", "good", "--local", "--target", str(tmp_path / "t"), "--repo", "a/b"]) == 2
    assert "patch not found" in capsys.readouterr().err


def test_demo_reset_requires_yes(monkeypatch, tmp_path, capsys):
    db = tmp_path / "prm.db"
    db.write_text("x")
    monkeypatch.setenv("PRM_DB", str(db))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    assert main(["demo", "reset"]) == 2
    assert db.exists() and "refusing" in capsys.readouterr().err
    assert main(["demo", "reset", "--yes"]) == 0
    assert not db.exists()


def test_demo_reset_refuses_database_url(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://h/db")
    assert main(["demo", "reset", "--yes"]) == 2


A, B = "PRM_TEST_DOTENV_A", "PRM_TEST_DOTENV_B"


@pytest.fixture
def dotenv_dir(tmp_path, monkeypatch):
    for name in (A, B, "PRM_NO_DOTENV"):
        monkeypatch.delenv(name, raising=False)  # restored at teardown, even after load_dotenv sets them
    (tmp_path / DOTENV_FILE).write_text(f"{A}=from-file\n{B}=from-file\n")
    return tmp_path


def test_local_env_fills_gaps_but_never_overrides_the_shell(dotenv_dir, monkeypatch):
    monkeypatch.setenv(B, "from-shell")
    assert load_local_env(dotenv_dir) is True
    assert os.environ[A] == "from-file" and os.environ[B] == "from-shell"


def test_local_env_opt_out_and_missing_file(dotenv_dir, monkeypatch, tmp_path_factory):
    assert load_local_env(tmp_path_factory.mktemp("empty")) is False
    monkeypatch.setenv("PRM_NO_DOTENV", "1")
    assert load_local_env(dotenv_dir) is False and A not in os.environ


def test_main_with_argv_never_loads_the_local_env(dotenv_dir, monkeypatch):
    monkeypatch.chdir(dotenv_dir)
    assert main(["validate", "--server", str(ROOT / "prm.server.toml")]) == 0
    assert A not in os.environ
