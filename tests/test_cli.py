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
