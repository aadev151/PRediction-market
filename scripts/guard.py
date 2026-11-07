#!/usr/bin/env python3
"""Claude Code PreToolUse hook: keep agents away from holdouts, secrets and the repo's guard rails.

Blocks (exit 2, reason on stderr) when a tool call:
- touches `.league/`, `.prm-cache/`, or a `*holdouts*` path outside `examples/` (any tool);
- reads a secret: `.env*` files (`.env.example` is fine), `$*_TOKEN` / `$*_SECRET` / `$*_API_KEY`
  expansions, such names inside inline code (`python -c`, code heredocs), env dumps (`printenv`, bare
  `env`/`set`/`export`/`declare`), `gh auth token`, `git credential fill`;
- edits CI, semgrep or hook config: `.github/workflows/`, `.semgrep/`, `.semgrepignore`, `.claude/`
  (and any `.claude/settings*.json`), `.githooks/`, or the hook scripts, via the edit tools or Bash
  writes (redirects, rm/mv/cp/tee/sed -i/...);
- deletes tests (`rm`/`git rm`/`find -delete` on `tests/` or `test_*.py`) or skips checks
  (`--no-verify`, `--deselect`, `-k "not ..."`, `pytest --ignore`).
Prose (commit messages, PR bodies, echo, text written via `cat > file <<EOF`) is not path-checked.
Unparseable input exits 0 (nothing to judge); an internal error exits 2 (fail closed).
Kept Python 3.9-compatible because it runs under whatever `python3` is on PATH.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import sys

SENSITIVE_VAR = r"[A-Za-z_][A-Za-z0-9_]*_(?:TOKEN|SECRET|API_KEY)"
SECRET_NAME = re.compile(rf"\b{SENSITIVE_VAR}\b")
SECRET_EXPANSION = re.compile(rf"\$\{{?[#!]?{SENSITIVE_VAR}\b")
ENV_FILE = re.compile(r"(?:^|[^\w.-])(\.env(?:\.[\w-]+)*)(?![\w-])", re.IGNORECASE)
FORBIDDEN_DIR = re.compile(r"(?:^|[^\w-])\.(?:league|prm-cache)(?![\w-])", re.IGNORECASE)
HOLDOUTS_WORD = re.compile(r"[\w.~/*?\[\]{}-]*holdouts(?!_)[\w.~/*?\[\]{}-]*", re.IGNORECASE)
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][\w-]*)\1")
INTERPRETER = re.compile(r"^(python[\d.]*|node|ruby|perl|php|bash|sh|zsh|dash|ksh|deno|bun)$")
WRAPPERS = {"sudo", "command", "env", "time", "nohup", "exec", "xargs"}
MESSAGE_FLAGS = {"-m", "--message", "-b", "--body", "-t", "--title", "--notes", "--subject"}
MUTATORS = {"rm", "rmdir", "unlink", "shred", "mv", "tee", "truncate", "touch", "chmod", "chown"}
DELETERS = {"rm", "rmdir", "unlink", "shred"}
GUARD_SCRIPTS = ("scripts/guard.py", "scripts/git_guard.py", "scripts/tldr_check.py")
PATH_KEYS = ("file_path", "notebook_path", "path")
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}


def _parts(path: str, cwd: str) -> list[str]:
    full = os.path.normpath(os.path.join(cwd, os.path.expanduser(path)))
    return [p.lower() for p in full.split(os.sep) if p]


def forbidden_path(path: str, cwd: str) -> str | None:
    """Holdouts, league ground truth, caches and .env files."""
    parts = _parts(path, cwd)
    for i, part in enumerate(parts):
        if part in (".league", ".prm-cache"):
            return f"`{path}` is under {part}/ (league ground truth / holdout cache)."
        # A holdouts dir (or archive/glob) outside examples/. Factory source like
        # prm/settle/holdouts.py or tests/settle/test_holdouts.py is not a holdouts path.
        is_source = i == len(parts) - 1 and part.endswith(".py")
        if "holdouts" in part and not is_source and "examples" not in parts[:i]:
            return f"`{path}` is a holdouts path outside examples/."
    name = parts[-1] if parts else ""
    if (name == ".env" or name.startswith(".env.")) and name != ".env.example":
        return f"`{path}` holds secrets; only .env.example may be read."
    return None


def protected_path(path: str, cwd: str) -> str | None:
    """CI, semgrep and hook config, which agents must not change."""
    parts = _parts(path, cwd)
    joined = "/".join(parts)
    home = _parts("~", "/")
    for i, part in enumerate(parts):
        if part == ".github" and parts[i + 1 : i + 2] == ["workflows"]:
            return f"`{path}` is CI config (.github/workflows/)."
        if part in (".semgrep", ".githooks", ".semgrepignore"):
            return f"`{path}` is semgrep/git-hook config ({part})."
        if part == ".claude" and (parts[:i] != home or parts[-1].startswith("settings")):
            return f"`{path}` is Claude Code hook config (.claude/)."
    if any(joined.endswith(s) for s in GUARD_SCRIPTS):
        return f"`{path}` is a repo guard hook."
    return None


def check_text(text: str, cwd: str, prose: bool = False, code: bool = False) -> str | None:
    """Path and secret checks on one argument (or a heredoc body)."""
    if not prose:
        if FORBIDDEN_DIR.search(text):
            return "references .league/ or .prm-cache/ (league ground truth / holdout cache)."
        for m in ENV_FILE.finditer(text):
            if m.group(1).lower() != ".env.example":
                return f"references `{m.group(1)}`, which holds secrets; only .env.example may be read."
    for word in HOLDOUTS_WORD.findall(text):
        if not prose or "/" in word:
            reason = forbidden_path(word, cwd)
            if reason:
                return reason
    if code and SECRET_NAME.search(text):
        return "inline code mentions a secret env var (*_TOKEN / *_SECRET / *_API_KEY)."
    return None


def split_heredocs(command: str) -> tuple[str, list[tuple[str, str]]]:
    """Remove heredoc bodies. Returns (command without bodies, [(line prefix, body)])."""
    lines = command.split("\n")
    out: list[str] = []
    bodies: list[tuple[str, str]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        i += 1
        for m in HEREDOC.finditer(line):
            body: list[str] = []
            while i < len(lines) and lines[i].strip() != m.group(2):
                body.append(lines[i])
                i += 1
            i += 1
            bodies.append((line[: m.start()], "\n".join(body)))
    return "\n".join(out), bodies


def tokenize(command: str) -> list[list[str]]:
    """Split into simple commands, keeping redirect operators (`>`, `>>`, `<`) as tokens."""
    try:
        lex = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        tokens = list(lex)
    except ValueError:
        tokens = command.split()
    segments: list[list[str]] = [[]]
    for tok in tokens:
        if tok and all(c in "();|&" for c in tok):
            segments.append([])
        else:
            segments[-1].append(tok)
    return [s for s in segments if s]


def _argv(seg: list[str]) -> list[str]:
    i = 0
    while i < len(seg) and (re.match(r"^\w+=", seg[i]) or seg[i] in WRAPPERS or seg[i].startswith("$(")):
        i += 1
    return seg[i:]


def _heredoc_cmd(prefix: str) -> str:
    last = re.split(r"[;&|(]|\$\(", prefix)[-1].split()
    return os.path.basename(last[0]) if last else ""


def check_segment(seg: list[str], cwd: str) -> str | None:
    if [t for t in seg if not re.match(r"^\w+=", t)] in (["env"], ["sudo", "env"]):
        return "`env` dumps environment variables (secrets)."
    argv = _argv(seg)
    if not argv:
        return None
    cmd = os.path.basename(argv[0])
    code = any(INTERPRETER.match(os.path.basename(t)) for t in argv[:3])
    args = argv[1:]
    pytest = any("pytest" in os.path.basename(t) for t in argv[:3])

    if cmd == "printenv" or (cmd in ("set", "export", "declare") and not args):
        return f"`{cmd}` dumps environment variables (secrets)."
    if cmd in ("export", "declare") and any(a in ("-p", "-x") for a in args):
        return f"`{cmd} {args[0]}` dumps environment variables (secrets)."
    if (cmd == "gh" and args[:1] == ["auth"] and "token" in args[1:2]) or "--show-token" in args:
        return "prints the GitHub token."
    if cmd == "git" and args[:2] == ["credential", "fill"]:
        return "prints git credentials."

    sub = args[0] if cmd == "git" and args else ""
    writes: list[str] = []
    prev = ""
    for tok in argv:
        prose = (
            " " in tok
            and (prev in MESSAGE_FLAGS or cmd in ("echo", "printf"))
            or bool(re.match(r"^--(message|body|title)=", tok))
        )
        reason = check_text(tok, cwd, prose=prose, code=code)
        if reason:
            return reason
        if prev and re.fullmatch(r"\d*&?>{1,2}\|?", prev):
            writes.append(tok)
        if tok == "--no-verify":
            return "`--no-verify` bypasses the repo's git hooks."
        if tok == "--deselect" or tok.startswith("--deselect="):
            return "`--deselect` skips tests."
        if (prev == "-k" or tok.startswith("-k")) and re.search(r"\bnot\b", tok.lower()):
            return '`-k "not ..."` skips tests.'
        if pytest and re.match(r"^--ignore(-glob)?(=|$)", tok):
            return "`pytest --ignore` skips tests."
        prev = tok

    targets = [a for a in args if not a.startswith("-")]
    if cmd in ("cp", "ln", "install"):
        writes += targets[-1:]
    elif cmd in MUTATORS or (cmd in ("sed", "perl") and any(a.startswith("-i") for a in args)):
        writes += targets
    if cmd == "git" and sub in ("rm", "mv"):
        targets = targets[1:]
        writes += targets
    if cmd == "find" and ("-delete" in args or "rm" in args):
        writes += targets
    for path in writes:
        reason = protected_path(path, cwd)
        if reason:
            return f"writes to protected config: {reason}"
    deleting = cmd in DELETERS or (cmd == "git" and sub == "rm") or (cmd == "find" and "-delete" in args)
    if deleting:
        for path in targets:
            parts = _parts(path, cwd)
            name = parts[-1] if parts else ""
            if "tests" in parts or re.match(r"^(test_.*|.*_test)\.py$|^conftest\.py$", name):
                return f"deletes tests (`{path}`)."
    return None


def evaluate_bash(command: str, cwd: str) -> str | None:
    if SECRET_EXPANSION.search(command):
        return "expands a secret env var ($*_TOKEN / $*_SECRET / $*_API_KEY)."
    if re.search(r"/proc/[^/\s]+/environ", command):
        return "reads a process environment (secrets)."
    main_text, bodies = split_heredocs(command)
    for prefix, body in bodies:
        writes_file = _heredoc_cmd(prefix) in ("cat", "tee")
        reason = check_text(body, cwd, prose=writes_file, code=not writes_file)
        if reason:
            return f"heredoc {reason}"
    for seg in tokenize(main_text):
        argv = _argv(seg)
        if argv[:1] == ["cd"] and len(argv) > 1:
            cwd = os.path.join(cwd, os.path.expanduser(argv[1]))
            continue
        reason = check_segment(seg, cwd)
        if reason:
            return reason
    return None


def evaluate(tool: str, tool_input: dict, cwd: str) -> str | None:
    """Return a reason to block this tool call, or None to allow it."""
    if tool == "Bash":
        return evaluate_bash(str(tool_input.get("command") or ""), cwd)
    paths = [str(tool_input[k]) for k in PATH_KEYS if tool_input.get(k)]
    base = paths[-1] if tool in ("Grep", "Glob") and paths else cwd
    if tool == "Glob" and tool_input.get("pattern"):
        paths.append(os.path.join(base, str(tool_input["pattern"])))
    if tool == "Grep" and tool_input.get("glob"):
        paths.append(os.path.join(base, str(tool_input["glob"])))
    for path in paths:
        reason = forbidden_path(path, cwd) or check_text(path, cwd)
        if reason:
            return reason
        if tool in EDIT_TOOLS:
            reason = protected_path(path, cwd)
            if reason:
                return f"edits protected config: {reason}"
    return None


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (ValueError, OSError):
        return 0
    if not isinstance(data, dict):
        return 0
    tool_input = data.get("tool_input")
    try:
        reason = evaluate(
            str(data.get("tool_name") or ""),
            tool_input if isinstance(tool_input, dict) else {},
            str(data.get("cwd") or os.getcwd()),
        )
    except Exception as exc:  # fail closed: a guard that crashes must not let the call through
        reason = f"guard error ({type(exc).__name__}); blocking to be safe."
    if reason:
        print(f"BLOCKED by repo rule (scripts/guard.py): {reason}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
