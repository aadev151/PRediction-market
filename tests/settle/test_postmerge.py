import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from prm.settle.postmerge import MergedPR, evaluate, settle_postmerge, window_end

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


@pytest.fixture
def repo(tmp_path: Path) -> Repo:
    return Repo(tmp_path)


@pytest.fixture
def pr(repo: Repo) -> MergedPR:
    """A squash-merged PR that adds b()."""
    return MergedPR(repo.commit({"app.py": BASE + PR_ADDS}, "Add b (#7)", T0), T0)


def days(n: float) -> datetime:
    return T0 + timedelta(days=n)


def test_quiet_pr_is_pending_in_the_window_and_yes_when_fast_forwarded(repo: Repo, pr: MergedPR) -> None:
    v = evaluate(repo.path, pr, now=days(1))
    assert (v.status, v.reason, v.settles_at) == ("pending", "in_window", window_end(T0, 14))
    v = evaluate(repo.path, pr, now=v.settles_at)  # the demo's "fast forward 14 days"
    assert (v.status, v.reason, v.culprit_sha) == ("yes", "window_elapsed", None)


def test_a_fix_to_an_added_line_settles_no_as_soon_as_observed(repo: Repo, pr: MergedPR) -> None:
    fix = repo.edit("return 2", "return 3", "Fix b off-by-one", days(2))
    assert evaluate(repo.path, pr, now=days(1)).status == "pending"  # not visible yet on the simulated clock
    v = evaluate(repo.path, pr, now=days(3))
    assert (v.status, v.reason, v.culprit_sha, v.observed_at) == ("no", "fixed", fix, days(2))


def test_a_fix_after_the_window_is_ignored(repo: Repo, pr: MergedPR) -> None:
    repo.edit("return 2", "return 3", "fix b", days(15))
    assert evaluate(repo.path, pr, now=days(20)).status == "yes"


def test_a_non_fix_touching_the_lines_counts_only_without_a_pattern(repo: Repo, pr: MergedPR) -> None:
    repo.edit("return 2", "return 2  # two", "Refactor b", days(2))
    assert evaluate(repo.path, pr, now=days(14)).status == "yes"
    assert evaluate(repo.path, pr, now=days(14), fix_pattern=None).reason == "fixed"


def test_fixes_elsewhere_new_lines_and_renames_do_not_count(repo: Repo, pr: MergedPR) -> None:
    repo.edit("return 1", "return 0", "fix a", days(1))  # a line the PR didn't write
    repo.edit("return 2\n", "return 2\n    # bugfix: note\n", "bugfix: add a note", days(2))  # insert only
    repo.git("mv", "app.py", "core.py")
    repo.git("commit", "-q", "-m", "fix: move app to core", when=days(3))
    assert evaluate(repo.path, pr, now=days(14)).status == "yes"


def test_git_revert_settles_no(repo: Repo, pr: MergedPR) -> None:
    repo.git("revert", "--no-edit", pr.merge_sha, when=days(1))
    v = evaluate(repo.path, pr, now=days(2))
    assert (v.status, v.reason) == ("no", "reverted")


def test_revert_of_a_deletion_only_pr_is_caught_by_its_message(repo: Repo) -> None:
    merge = repo.commit({"app.py": "def a():\n"}, "Drop return", T0)
    repo.git("revert", "--no-edit", merge[:10], when=days(1))
    assert evaluate(repo.path, MergedPR(merge, T0), now=days(2)).reason == "reverted"


def test_merge_commit_pr_lines_blame_to_the_branch_commits(repo: Repo) -> None:
    repo.git("switch", "-q", "-c", "feature")
    repo.commit({"app.py": BASE + PR_ADDS}, "Add b", T0 - timedelta(hours=2))
    repo.git("switch", "-q", "main")
    repo.git("merge", "-q", "--no-ff", "-m", "Merge #8", "feature", when=T0)
    pr = MergedPR(repo.git("rev-parse", "HEAD"), T0)
    repo.edit("return 2", "return -2", "hotfix sign of b", days(1))
    assert evaluate(repo.path, pr, now=days(2)).reason == "fixed"


def test_settle_postmerge_maps_to_the_engine_contract(repo: Repo, pr: MergedPR) -> None:
    assert settle_postmerge(repo.path, pr, now=days(1)) is None
    result = settle_postmerge(repo.path, pr, now=days(14))
    assert result is not None
    assert (result.outcome, result.holdouts_passed, result.holdouts_total) == ("YES", 0, 0)
    assert result.error is None
    repo.edit("return 2", "return 3", "fix b", days(15))
    assert settle_postmerge(repo.path, pr, now=days(16), window_days=30).outcome == "NO"


def test_git_failures_fail_closed(repo: Repo, pr: MergedPR, tmp_path: Path) -> None:
    missing = settle_postmerge(tmp_path / "nope", pr, now=days(14))
    assert missing is not None and missing.outcome is None and missing.error
    repo.git("switch", "-q", "-c", "other", f"{pr.merge_sha}^")
    off_branch = settle_postmerge(repo.path, pr, now=days(14))  # HEAD no longer contains the merge
    assert off_branch is not None and off_branch.outcome is None
    unknown = MergedPR("0" * 40, T0)
    assert settle_postmerge(repo.path, unknown, now=days(14)).outcome is None
    assert settle_postmerge(repo.path, pr, now=days(14), branch="--output=/tmp/x").outcome is None


def test_inputs_are_validated() -> None:
    with pytest.raises(ValueError):
        MergedPR("--all", T0)
    with pytest.raises(ValueError):
        MergedPR("a" * 40, datetime(2026, 11, 1))  # naive
