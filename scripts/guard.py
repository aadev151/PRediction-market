#!/usr/bin/env python3
"""Claude Code PreToolUse hook: keep agents away from holdouts, secrets and the repo's guard rails.

Blocks (exit 2, reason on stderr) when a tool call:
- touches `.league/`, `.prm-cache/`, or a `*holdouts*` path outside `examples/` (any tool), including
  via shell globs / brace expansion (`.le*`, `{.league,x}`) and inline interpreter code that names
  them, even split (`'.lea'+'gue'`);
- reads a secret: `.env*` files (`.env.example` is fine), credential stores (`~/.netrc`,
  `~/.git-credentials`, `~/.config/gh/`, `~/.aws/credentials`, `~/.docker/config.json`, `~/.pypirc`,
  `~/.anthropic`, ...), the macOS keychain (`security find-*-password`), `$*_TOKEN` / `$*_SECRET` /
  `$*_API_KEY` expansions, such names inside inline code, env dumps (`printenv`, bare
  `env`/`set`/`export`/`declare`), `gh auth token`, `git credential fill`;
- edits CI, semgrep or hook config: `.github/workflows/`, `.semgrep/`, `.semgrepignore`, `.claude/`
  (and any `.claude/settings*.json`), `.githooks/`, or the hook scripts, via the edit tools or Bash
  writes (redirects, rm/mv/cp/tee/sed -i/...);
- deletes, empties or reverts tests (`rm`/`git rm`/`find -delete`, `truncate`, `: > test_x.py`,
  `cp /dev/null`, `git checkout <rev> -- tests/`, `git restore --source`) or skips checks
  (`--no-verify`, `--deselect`, `-k "not ..."`, `-m "not ..."`, `pytest --ignore`).
Prose (commit messages, PR bodies, echo, text written via `cat > file <<EOF`) is not path-checked.
Unparseable input exits 0 (nothing to judge); an internal error exits 2 (fail closed).
Kept Python 3.9-compatible because it runs under whatever `python3` is on PATH.

Known limits (a regex hook can't close these; CI, review and the private holdouts repo do):
- Indirection: a script file, Makefile target or `git apply` patch that reads or deletes things is
  judged by its command line only, not by what the file does.
- Obfuscated code (`chr()` arithmetic, base64, env-var indirection) can name a path without spelling it.
- Weakening a test with Edit/Write (e.g. replacing its body with `pass`) looks like a legit test edit.
- Holdout scenario *names* can leak through broad listings (`ls`, `**/*` globs) of a dir not named
  holdouts. Real holdouts live in a private repo that coding agents never get a token for.
"""

from __future__ import annotations

import fnmatch
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
CREDENTIALS = re.compile(
    r"(?:^|[\s/'\"=:~])(?:\.git-credentials|[._]netrc|\.pypirc|\.npmrc|\.anthropic)(?![\w-])|"
    r"\.aws/credentials|\.docker/config\.json|\.config/gh\b|\bgh/hosts\.yml|\.ssh/id_",
    re.IGNORECASE | re.MULTILINE,
)
HOLDOUTS_WORD = re.compile(r"[\w.~/*?\[\]{}-]*holdouts(?!_)[\w.~/*?\[\]{}-]*", re.IGNORECASE)
GLOB_WORD = re.compile(r"[\w.~/,*?\[\]{}!-]*[*?\[{][\w.~/,*?\[\]{}!-]*")
HEREDOC = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][\w-]*)\1")
INTERPRETER = re.compile(r"^(python[\d.]*|node|ruby|perl|php|bash|sh|zsh|dash|ksh|deno|bun)$")
WRAPPERS = {"sudo", "command", "env", "time", "nohup", "exec", "xargs"}
MESSAGE_FLAGS = {"-m", "--message", "-b", "--body", "-t", "--title", "--notes", "--subject"}
CODE_FLAGS = {"-c", "-e", "--eval", "-p", "--print", "-r"}
MUTATORS = {"rm", "rmdir", "unlink", "shred", "mv", "tee", "truncate", "touch", "chmod", "chown"}
DELETERS = {"rm", "rmdir", "unlink", "shred"}
GUARD_SCRIPTS = ("scripts/guard.py", "scripts/git_guard.py", "scripts/tldr_check.py")
PATH_KEYS = ("file_path", "notebook_path", "path")
EDIT_TOOLS = {"Edit", "Write", "MultiEdit", "NotebookEdit"}
HOLDOUT_SAMPLES = ("holdouts", "acme-holdouts", "holdouts-acme", "acme-holdouts-acme")
# Mentions in inline code that are factory source, not data: prm.league, prm/settle/holdouts.py, ...
CODE_EXEMPT = re.compile(
    r"prm[./]league|league\.md|prm[./]settle[./]holdouts|holdouts\.py|holdouts_\w*|examples/[\w.-]*holdouts"
)


def _parts(path: str, cwd: str) -> list[str]:
    full = os.path.normpath(os.path.join(cwd, os.path.expanduser(path)))
    return [p.lower() for p in full.split(os.sep) if p]


def expand_braces(word: str, limit: int = 64) -> list[str]:
    """Shell brace expansion (`{a,b}`), innermost first, capped."""
    out, todo = [], [word]
    while todo and len(out) + len(todo) <= limit:
        w = todo.pop()
        m = re.search(r"\{([^{}]*,[^{}]*)\}", w)
        if not m:
            out.append(w)
            continue
        todo += [w[: m.start()] + alt + w[m.end() :] for alt in m.group(1).split(",")]
    return out + todo


def _glob_hit(part: str) -> str | None:
    """Can this glob component match .league, .prm-cache or a holdouts dir?"""
    if not re.search(r"[*?\[]", part):
        return None
    if part.startswith((".", "[.]")) and any(fnmatch.fnmatchcase(n, part) for n in (".league", ".prm-cache")):
        return "league ground truth / holdout cache"
    literals = re.sub(r"\[[^\]]*\]|[*?]", "", part)
    if len(literals) >= 3 and any(fnmatch.fnmatchcase(n, part) for n in HOLDOUT_SAMPLES):
        return "holdouts"
    return None


def forbidden_path(path: str, cwd: str) -> str | None:
    """Holdouts, league ground truth, caches, .env files and credential stores (globs expanded)."""
    for alt in expand_braces(path):
        parts = _parts(alt, cwd)
        for i, part in enumerate(parts):
            if part in (".league", ".prm-cache"):
                return f"`{path}` is under {part}/ (league ground truth / holdout cache)."
            # A holdouts dir (or archive/glob) outside examples/. Factory source like
            # prm/settle/holdouts.py, tests/settle/test_holdouts.py or the module name
            # prm.settle.holdouts is not a holdouts path.
            is_source = i == len(parts) - 1 and (part.endswith(".py") or part.startswith(("prm.", "tests.")))
            outside = "examples" not in parts[:i]
            if "holdouts" in part and not is_source and outside:
                return f"`{path}` is a holdouts path outside examples/."
            hit = _glob_hit(part)
            if hit and (outside or hit != "holdouts"):
                return f"`{path}` is a glob that can match {hit} paths."
        name = parts[-1] if parts else ""
        if (name == ".env" or name.startswith(".env.")) and name != ".env.example":
            return f"`{path}` holds secrets; only .env.example may be read."
        if CREDENTIALS.search("/" + "/".join(parts)):
            return f"`{path}` is a credential store."
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


def is_test_path(path: str, cwd: str) -> bool:
    parts = _parts(path, cwd)
    name = parts[-1] if parts else ""
    return "tests" in parts or bool(re.match(r"^(test_.*|.*_test)\.py$|^conftest\.py$", name))


def check_code(code: str) -> str | None:
    """Inline interpreter code: names of secrets or forbidden dirs, even split across literals."""
    if SECRET_NAME.search(code):
        return "inline code mentions a secret env var (*_TOKEN / *_SECRET / *_API_KEY)."
    flat = CODE_EXEMPT.sub("", re.sub(r"[\s'\"+\\]", "", code.lower()))
    for needle in ("league", "holdout", "prm-cache", "prm_cache"):
        if needle in flat:
            return f"inline code mentions `{needle}` (league ground truth / holdouts / cache)."
    return None


def check_text(text: str, cwd: str, prose: bool = False, code: bool = False) -> str | None:
    """Path and secret checks on one argument (or a heredoc body)."""
    if not prose:
        if FORBIDDEN_DIR.search(text):
            return "references .league/ or .prm-cache/ (league ground truth / holdout cache)."
        for m in ENV_FILE.finditer(text):
            if m.group(1).lower() != ".env.example":
                return f"references `{m.group(1)}`, which holds secrets; only .env.example may be read."
        if CREDENTIALS.search(text):
            return "references a credential store."
        for word in GLOB_WORD.findall(text):
            reason = forbidden_path(word, cwd)
            if reason:
                return reason
    for word in HOLDOUTS_WORD.findall(text):
        if not prose or "/" in word:
            reason = forbidden_path(word, cwd)
            if reason:
                return reason
    return check_code(text) if code else None


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


def _is_interpreter(words: list[str]) -> bool:
    return any(INTERPRETER.match(os.path.basename(t)) for t in words[:3])


def _heredoc_kind(prefix: str) -> str:
    """'code' (fed to an interpreter), 'prose' (written to a file / used as a message) or 'plain'."""
    words = re.split(r"[;&|(]|\$\(", prefix)[-1].split()
    if _is_interpreter(words):
        return "code"
    if words and os.path.basename(words[0]) in ("cat", "tee", "gh", "git"):
        return "prose"
    return "plain"


def check_tests_weakened(cmd: str, sub: str, args: list[str], cwd: str) -> str | None:
    targets = [a for a in args if not a.startswith("-")]
    if cmd == "git" and sub in ("rm", "checkout", "restore"):
        targets = targets[1:]
        if sub == "restore" and any(a in ("--staged", "-S") for a in args):
            if not any(a.startswith(("--source", "-s", "--worktree", "-W")) for a in args):
                targets = []  # unstaging only
    deleting = cmd in DELETERS or (cmd == "git" and sub == "rm") or (cmd == "find" and "-delete" in args)
    for path in targets:
        if not is_test_path(path, cwd):
            continue
        if deleting:
            return f"deletes tests (`{path}`)."
        if cmd == "git" and sub in ("checkout", "restore"):
            return f"reverts tests to another version (`{path}`)."
        if cmd == "truncate" or (cmd in ("cp", "install") and "/dev/null" in targets[:-1]):
            return f"empties a test file (`{path}`)."
    if cmd == "dd" and any(a.startswith("of=") and is_test_path(a[3:], cwd) for a in args):
        return "overwrites a test file with dd."
    return None


def check_segment(seg: list[str], cwd: str) -> str | None:
    if [t for t in seg if not re.match(r"^\w+=", t)] in (["env"], ["sudo", "env"]):
        return "`env` dumps environment variables (secrets)."
    argv = _argv(seg)
    if not argv:
        return None
    cmd = os.path.basename(argv[0])
    interpreter = _is_interpreter(argv)
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
    if cmd == "security" and args and re.match(r"^(find-\w+-password|dump-keychain|export)$", args[0]):
        return "reads the macOS keychain."

    sub = args[0] if cmd == "git" and args else ""
    writes: list[str] = []
    truncates: list[str] = []
    content: list[str] = []
    prev = ""
    for tok in argv:
        prose = (
            " " in tok
            and (prev in MESSAGE_FLAGS or cmd in ("echo", "printf"))
            or bool(re.match(r"^--(message|body|title)=", tok))
        )
        reason = check_text(tok, cwd, prose=prose, code=interpreter and prev in CODE_FLAGS)
        if reason:
            return reason
        redirect = re.fullmatch(r"\d*&?>{1,2}\|?", tok)
        if prev and re.fullmatch(r"\d*&?>{1,2}\|?", prev):
            writes.append(tok)
            if ">>" not in prev:
                truncates.append(tok)
        elif not redirect:
            content.append(tok)
        if tok == "--no-verify":
            return "`--no-verify` bypasses the repo's git hooks."
        if tok == "--deselect" or tok.startswith("--deselect="):
            return "`--deselect` skips tests."
        if (prev == "-k" or tok.startswith("-k")) and re.search(r"\bnot\b", tok.lower()):
            return '`-k "not ..."` skips tests.'
        if pytest and prev == "-m" and re.search(r"\bnot\b", tok.lower()):
            return '`pytest -m "not ..."` deselects tests by marker.'
        if pytest and re.match(r"^--ignore(-glob)?(=|$)", tok):
            return "`pytest --ignore` skips tests."
        prev = tok

    # `> tests/test_x.py`, `: > ...`, `echo > ...`, `cat /dev/null > ...` empty the file.
    empty_echo = content[:1] in (["echo"], ["printf"]) and all(c in ("", "-n") for c in content[1:])
    if not content or content[:1] in ([":"], ["true"]) or empty_echo or content == ["cat", "/dev/null"]:
        for path in truncates:
            if is_test_path(path, cwd):
                return f"empties a test file (`{path}`)."

    targets = [a for a in args if not a.startswith("-")]
    if cmd in ("cp", "ln", "install"):
        writes += targets[-1:]
    elif cmd in MUTATORS or (cmd in ("sed", "perl") and any(a.startswith("-i") for a in args)):
        writes += targets
    if cmd == "git" and sub in ("rm", "mv"):
        writes += targets[1:]
    if cmd == "find" and ("-delete" in args or "rm" in args):
        writes += targets
    for path in writes:
        reason = protected_path(path, cwd)
        if reason:
            return f"writes to protected config: {reason}"
    return check_tests_weakened(cmd, sub, args, cwd)


def evaluate_bash(command: str, cwd: str) -> str | None:
    if SECRET_EXPANSION.search(command):
        return "expands a secret env var ($*_TOKEN / $*_SECRET / $*_API_KEY)."
    if re.search(r"/proc/[^/\s]+/environ", command):
        return "reads a process environment (secrets)."
    main_text, bodies = split_heredocs(command)
    for prefix, body in bodies:
        kind = _heredoc_kind(prefix)
        reason = check_text(body, cwd, prose=kind == "prose", code=kind == "code")
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
