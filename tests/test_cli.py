from pathlib import Path

import pytest

from prm.cli import main

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
