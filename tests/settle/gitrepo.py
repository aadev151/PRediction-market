"""Throwaway git repos for settlement tests (pinned commit dates, no user git config)."""

import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

T0 = datetime(2026, 11, 1, 12, 0, tzinfo=UTC)  # the merge
BASE = "def a():\n    return 1\n"
PR_ADDS = "\n\ndef b():\n    return 2\n"


class Repo:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.git("init", "-q", "-b", "main")
        self.commit({"app.py": BASE}, "initial", T0 - timedelta(days=3))

    def git(self, *args: str, when: datetime | None = None) -> str:
        env = {"PATH": os.environ["PATH"], "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_NOSYSTEM": "1"}
        if when is not None:
            env["GIT_AUTHOR_DATE"] = env["GIT_COMMITTER_DATE"] = when.isoformat()
        cmd = ["git", "-C", str(self.path), "-c", "user.name=t", "-c", "user.email=t@example.com", *args]
        return subprocess.run(cmd, env=env, check=True, capture_output=True, text=True).stdout.strip()

    def commit(self, files: dict[str, str], message: str, when: datetime) -> str:
        for name, text in files.items():
            (self.path / name).write_text(text)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", message, when=when)
        return self.git("rev-parse", "HEAD")

    def edit(self, old: str, new: str, message: str, when: datetime) -> str:
        text = (self.path / "app.py").read_text()
        return self.commit({"app.py": text.replace(old, new)}, message, when)
