"""WP D. A minimal git runner for settlement: ``shell=False``, isolated config, no factory secrets."""

from __future__ import annotations

import os
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path

GIT_TIMEOUT_S = 60


class GitError(RuntimeError):
    """git failed or the history doesn't fit what we expected. VOIDs the market.

    Messages carry the git subcommand and exit code only, never argv, URLs, stderr or credentials.
    """


class Git:
    def __init__(self, repo: Path, extra_env: Mapping[str, str] | None = None) -> None:
        exe = shutil.which("git")
        if exe is None:
            raise GitError("git is not installed")
        self._base = [exe, "-C", str(repo), "-c", "core.quotePath=false", "--no-pager"]
        # No factory secrets, no user/system config (aliases, pagers, credential helpers), never prompt.
        self._env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "LC_ALL": "C",
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            **(extra_env or {}),
        }

    def run(
        self, *args: str, check: bool = True, timeout_s: int = GIT_TIMEOUT_S
    ) -> subprocess.CompletedProcess[str]:
        try:
            proc = subprocess.run(  # noqa: S603 (fixed argv, shell=False, callers validate shas and refs)
                [*self._base, *args], capture_output=True, encoding="utf-8", errors="replace",
                env=self._env, timeout=timeout_s, check=False,
            )  # fmt: skip
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise GitError(f"git {args[0]}: {type(exc).__name__}") from exc
        if check and proc.returncode != 0:
            raise GitError(f"git {args[0]} exited {proc.returncode}")
        return proc

    def out(self, *args: str) -> str:
        return self.run(*args).stdout
