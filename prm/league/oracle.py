"""WP G. League ground truth: run the target's hidden tests on a mutant's tree (DECISIONS #66).

League mutants are never merged, so the post-merge oracle (#57) can't settle them. R-LG-2 scores every league
market against its hidden test result instead, and this module is the only place that runs them.

The command is the target's ``[settle].command``, rendered with ``render_command`` and run with
``shell=False`` in a scrubbed environment (no factory secrets). Counts only: no test names leave this module.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from prm.config import ConfigError, TargetConfig, render_command, render_env
from prm.types import SettlementResult

MAX_JUNIT_BYTES = 5_000_000
_SUITE = re.compile(r"<testsuite\s([^>]*)>")
_COUNT = re.compile(r"\b(tests|failures|errors)=\"(\d{1,9})\"")


def _elapsed_ms(t0: float) -> int:
    return max(0, int((time.monotonic() - t0) * 1000))


def parse_junit_counts(text: str) -> tuple[int, int] | None:
    """(total, failed) summed over every ``<testsuite>`` element; None if there is none.

    A tight regex over three integer attributes, not an XML parser: the file is written by code under test.
    """
    total = failed = 0
    found = False
    for attrs in _SUITE.findall(text):
        counts = dict(_COUNT.findall(attrs))
        if "tests" not in counts:
            continue
        found = True
        total += int(counts["tests"])
        failed += int(counts.get("failures", 0)) + int(counts.get("errors", 0))
    return (total, min(failed, total)) if found else None


def _read_junit(path: Path) -> tuple[int, int] | None:
    try:
        if not path.is_file() or path.stat().st_size > MAX_JUNIT_BYTES:
            return None
        return parse_junit_counts(path.read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return None


def scrubbed_env(tmp: Path, extra: dict[str, str]) -> dict[str, str]:
    """PATH (this interpreter's bin first), LANG, HOME, TMPDIR, plus the target's ``[settle]`` env table."""
    path = os.pathsep.join([os.path.dirname(sys.executable), os.environ.get("PATH", "/usr/bin:/bin")])
    env = {
        "PATH": path,
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "HOME": str(tmp),
        "TMPDIR": str(tmp),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    env.update(extra)
    return env


def run_tests(head_dir: Path, target: TargetConfig, target_root: Path) -> SettlementResult:
    """Run the hidden tests on ``head_dir``. YES iff at least one test ran and none failed or errored.

    Missing hidden tests, a bad command template or a command that can't start: infra error (no outcome).
    A timeout or crash of the code under test: NO. A missing or unreadable JUnit file: the exit code decides.
    """
    t0 = time.monotonic()
    tests_dir = target.holdouts_dir(Path(target_root))
    if tests_dir is None or not tests_dir.is_dir():
        return SettlementResult.infra_error("hidden tests not found", _elapsed_ms(t0))
    with tempfile.TemporaryDirectory(prefix="prm-league-") as tmp_str:
        tmp = Path(tmp_str)
        junit = tmp / "junit.xml"
        values = {"head": Path(head_dir).resolve(), "holdouts": tests_dir, "junit": junit, "tmp": tmp}
        try:
            argv = render_command(target.settle.command, values)
            env = scrubbed_env(tmp, render_env(target.settle.env, values))
        except (ConfigError, ValueError) as exc:
            return SettlementResult.infra_error(f"bad settle command: {type(exc).__name__}", _elapsed_ms(t0))
        try:
            proc = subprocess.run(  # noqa: S603 - argv from render_command, shell=False, scrubbed env
                argv, shell=False, cwd=head_dir, env=env, stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=target.settle.timeout_s, check=False,
            )  # fmt: skip
        except subprocess.TimeoutExpired:
            return SettlementResult("NO", 0, 0, [], _elapsed_ms(t0), None)
        except OSError as exc:
            return SettlementResult.infra_error(f"cannot run tests: {type(exc).__name__}", _elapsed_ms(t0))
        counts = _read_junit(junit)
    if counts is None:
        outcome = "YES" if proc.returncode == 0 else "NO"
        return SettlementResult(outcome, 0, 0, [], _elapsed_ms(t0), None)
    total, failed = counts
    outcome = "YES" if total > 0 and failed == 0 and proc.returncode == 0 else "NO"
    return SettlementResult(outcome, total - failed, total, [], _elapsed_ms(t0), None)
