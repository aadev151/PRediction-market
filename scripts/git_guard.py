#!/usr/bin/env python3
"""Claude Code PreToolUse hook (Bash): enforce the branch workflow.

Blocks, with exit code 2 and a reason on stderr:
- commit / merge / cherry-pick / revert / am / rebase while on main;
- any push that targets main (explicit refspec, `--all`, `--mirror`, or a bare push from main);
- bypassing the git hooks: `--no-verify`, `git commit -n`, or pointing `core.hooksPath` anywhere
  but `.githooks`.

Git hooks (.githooks/) and GitHub branch protection back this up; this hook catches it before the
command runs.
Kept Python 3.9-compatible because it runs under whatever `python3` is on PATH.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys

PROTECTED = {"main", "master"}
LOCAL_HISTORY_CMDS = {"commit", "merge", "cherry-pick", "revert", "am", "rebase"}
WRAPPERS = {"sudo", "command", "env", "time", "nohup", "exec"}
GIT_OPTS_WITH_VALUE = {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--exec-path", "--config-env"}
PUSH_OPTS_WITH_VALUE = {"-o", "--push-option", "--repo", "--receive-pack", "--exec"}
HOOKS_PATH = ".githooks"
BRANCH_HINT = "Create a branch first: git switch -c wp-<letter>/<topic>, then push it and open a PR."


def current_branch(cwd: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", "-C", cwd, "symbolic-ref", "--short", "-q", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def split_segments(command: str) -> list[list[str]]:
    """Split a shell command into simple commands at ; && || | & ( ) and newlines."""
    try:
        lex = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        tokens = command.split()
    segments: list[list[str]] = [[]]
    for tok in tokens:
        if tok and all(c in "();<>|&" for c in tok):
            segments.append([])
        else:
            segments[-1].append(tok)
    return [s for s in segments if s]


def _strip_prefix(seg: list[str]) -> list[str]:
    i = 0
    while i < len(seg) and ("=" in seg[i] and not seg[i].startswith("-") or seg[i] in WRAPPERS):
        i += 1
    return seg[i:]


def _norm_ref(ref: str) -> str:
    return ref[len("refs/heads/") :] if ref.startswith("refs/heads/") else ref


def check_push(args: list[str], branch: str | None) -> str | None:
    positionals: list[str] = []
    deleting = False
    i = 0
    while i < len(args):
        a = args[i]
        if a in ("--all", "--mirror"):
            return f"`git push {a}` would push main."
        if a in ("-d", "--delete"):
            deleting = True
        elif a in PUSH_OPTS_WITH_VALUE:
            i += 1
        elif not a.startswith("-"):
            positionals.append(a)
        i += 1
    refspecs = positionals[1:]
    if not refspecs:
        if branch in PROTECTED and not deleting:
            return f"a bare `git push` on '{branch}' pushes {branch}."
        return None
    for spec in refspecs:
        spec = spec.lstrip("+")
        dst = spec.split(":", 1)[1] if ":" in spec else spec
        if dst in ("HEAD", "@") and branch:
            dst = branch
        if _norm_ref(dst) in PROTECTED:
            return f"pushing to '{_norm_ref(dst)}' is not allowed."
    return None


def check_git(argv: list[str], cwd: str) -> str | None:
    i = 1
    while i < len(argv) and argv[i].startswith("-"):
        opt = argv[i]
        if opt in GIT_OPTS_WITH_VALUE and i + 1 < len(argv):
            value = argv[i + 1]
            if opt == "-C":
                cwd = os.path.join(cwd, value)
            if (
                opt == "-c"
                and value.lower().startswith("core.hookspath=")
                and value.split("=", 1)[1] != HOOKS_PATH
            ):
                return "overriding core.hooksPath bypasses the repo's git hooks."
            i += 2
            continue
        i += 1
    if i >= len(argv):
        return None
    sub, args = argv[i], argv[i + 1 :]

    if sub == "config":
        lowered = [a.lower() for a in args]
        if "core.hookspath" not in lowered:
            return None
        rest = args[lowered.index("core.hookspath") + 1 :]
        if any(a.startswith("--unset") for a in lowered) or (rest and rest[0] != HOOKS_PATH):
            return "changing core.hooksPath disables the repo's git hooks."
        return None  # reading it, or setting it to .githooks, is fine

    if sub in ("commit", "push", "merge", "cherry-pick", "revert", "am", "rebase", "pull"):
        if "--no-verify" in args:
            return "`--no-verify` bypasses the repo's git hooks."
        if sub == "commit" and any(
            a.startswith("-") and not a.startswith("--") and "n" in a[1:] for a in args
        ):
            return "`git commit -n` bypasses the repo's git hooks."

    branch = current_branch(cwd)
    if sub in LOCAL_HISTORY_CMDS and branch in PROTECTED:
        if sub == "commit" and "--dry-run" in args:
            return None
        return f"`git {sub}` on '{branch}' is not allowed."
    if sub == "push":
        return check_push(args, branch)
    return None


def evaluate(command: str, cwd: str) -> str | None:
    """Return a reason to block ``command``, or None to allow it."""
    for seg in split_segments(command):
        argv = _strip_prefix(seg)
        if not argv:
            continue
        if argv[0] == "cd" and len(argv) > 1:
            cwd = os.path.join(cwd, os.path.expanduser(argv[1]))
            continue
        if os.path.basename(argv[0]) == "git":
            reason = check_git(argv, cwd)
            if reason:
                return reason
    return None


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    if data.get("tool_name") != "Bash":
        return 0
    command = (data.get("tool_input") or {}).get("command") or ""
    cwd = data.get("cwd") or os.getcwd()
    reason = evaluate(command, cwd)
    if reason:
        print(f"BLOCKED by repo rule (scripts/git_guard.py): {reason}\n{BRANCH_HINT}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
