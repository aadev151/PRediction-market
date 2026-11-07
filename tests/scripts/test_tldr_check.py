import importlib.util
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("tldr_check", ROOT / "scripts" / "tldr_check.py")
tldr = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tldr)


def user(text):
    return {"type": "user", "message": {"role": "user", "content": text}}


def tool_result(tid, is_error=False):
    block = {"type": "tool_result", "tool_use_id": tid, "content": "ok", "is_error": is_error}
    return {"type": "user", "message": {"role": "user", "content": [block]}}


def bash(tid, command):
    block = {"type": "tool_use", "id": tid, "name": "Bash", "input": {"command": command}}
    return {"type": "assistant", "message": {"role": "assistant", "content": [block]}}


def say(text):
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def test_commit_without_tldr_is_blocked():
    entries = [user("build WP A"), bash("t1", "git commit -m 'wp-a'"), tool_result("t1"), say("Done.")]
    assert tldr.needs_tldr({}, entries)


def test_commit_with_tldr_passes():
    entries = [user("go"), bash("t1", "git push -u origin HEAD"), tool_result("t1"), say("**TL;DR**\n- ...")]
    assert not tldr.needs_tldr({}, entries)
    assert not tldr.needs_tldr({"last_assistant_message": "tldr: done"}, entries[:-1])


def test_no_git_write_no_requirement():
    entries = [user("explain LMSR"), bash("t1", "git status"), tool_result("t1"), say("It works like...")]
    assert not tldr.needs_tldr({}, entries)


def test_only_current_turn_counts():
    entries = [
        user("commit it"),
        bash("t1", "git commit -m x"),
        tool_result("t1"),
        say("TL;DR ..."),
        user("what does this do?"),
        say("It checks..."),
    ]
    assert not tldr.needs_tldr({}, entries)


def test_blocked_or_dry_run_commits_dont_count():
    entries = [user("go"), bash("t1", "git commit -m x"), tool_result("t1", is_error=True), say("blocked")]
    assert not tldr.needs_tldr({}, entries)
    entries = [user("go"), bash("t2", "git commit --dry-run"), tool_result("t2"), say("ok")]
    assert not tldr.needs_tldr({}, entries)


def test_stop_hook_active_prevents_loops():
    entries = [user("go"), bash("t1", "git commit -m x"), tool_result("t1"), say("Done.")]
    assert not tldr.needs_tldr({"stop_hook_active": True}, entries)


def test_hook_protocol(tmp_path):
    transcript = tmp_path / "t.jsonl"
    entries = [user("go"), bash("t1", "git commit -m x"), tool_result("t1"), say("Done.")]
    transcript.write_text("\n".join(json.dumps(e) for e in entries) + "\n")
    script = str(ROOT / "scripts" / "tldr_check.py")
    payload = json.dumps({"transcript_path": str(transcript), "stop_hook_active": False})
    res = subprocess.run([sys.executable, script], input=payload, capture_output=True, text=True)
    assert res.returncode == 2 and "TL;DR" in res.stderr
    res = subprocess.run([sys.executable, script], input="{}", capture_output=True, text=True)
    assert res.returncode == 0
