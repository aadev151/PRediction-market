#!/usr/bin/env python3
"""Claude Code Stop hook: a turn that committed or pushed must end with the TL;DR feature summary.

If the turn ran a successful `git commit` / `git push` and the final reply has no "TL;DR", exit 2 so Claude
continues and writes it. `stop_hook_active` prevents loops: it asks at most once per stop.
Anything unexpected (no transcript, bad JSON) allows the stop: this is a reminder, not a safety gate.
Kept Python 3.9-compatible because it runs under whatever `python3` is on PATH.
"""

from __future__ import annotations

import json
import re
import sys

GIT_WRITE = re.compile(r"\bgit\b[^\n;&|]*\s(commit|push)\b")
TLDR = re.compile(r"TL;?DR", re.IGNORECASE)

REMINDER = """Repo rule: you committed or pushed in this turn, so end with the TL;DR summary (CLAUDE.md):

**TL;DR**
- **What this feature is about:**
- **What I did:**
- **How it ties back to the overall project:**
- **How to test it:** (exact commands + expected output)
- **What's left to build:**"""


def _content(entry: dict) -> list:
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return content if isinstance(content, list) else []


def _is_user_prompt(entry: dict) -> bool:
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain"):
        return False
    return any(isinstance(b, dict) and b.get("type") == "text" for b in _content(entry))


def current_turn(entries: list[dict]) -> list[dict]:
    """Entries after the last real user prompt."""
    for i in range(len(entries) - 1, -1, -1):
        if _is_user_prompt(entries[i]):
            return entries[i + 1 :]
    return entries


def turn_wrote_git(turn: list[dict]) -> bool:
    failed = {
        b.get("tool_use_id")
        for e in turn
        for b in _content(e)
        if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("is_error")
    }
    for e in turn:
        if e.get("type") != "assistant" or e.get("isSidechain"):
            continue
        for b in _content(e):
            if not (isinstance(b, dict) and b.get("type") == "tool_use" and b.get("name") == "Bash"):
                continue
            cmd = (b.get("input") or {}).get("command") or ""
            if GIT_WRITE.search(cmd) and "--dry-run" not in cmd and b.get("id") not in failed:
                return True
    return False


def last_assistant_text(turn: list[dict]) -> str:
    for e in reversed(turn):
        if e.get("type") == "assistant" and not e.get("isSidechain"):
            texts = [
                b.get("text", "") for b in _content(e) if isinstance(b, dict) and b.get("type") == "text"
            ]
            if any(t.strip() for t in texts):
                return "\n".join(texts)
    return ""


def needs_tldr(data: dict, entries: list[dict]) -> bool:
    if data.get("stop_hook_active"):
        return False
    turn = current_turn(entries)
    if not turn_wrote_git(turn):
        return False
    text = data.get("last_assistant_message") or last_assistant_text(turn)
    return not TLDR.search(text or "")


def read_transcript(path: str) -> list[dict]:
    entries = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            try:
                entries.append(json.loads(line))
            except ValueError:
                continue
    return entries


def main() -> int:
    try:
        data = json.load(sys.stdin)
        entries = read_transcript(data["transcript_path"])
    except (ValueError, OSError, KeyError, TypeError):
        return 0
    if needs_tldr(data, entries):
        print(REMINDER, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
