"""Webhook helpers (PLAN §6.12): signature check, event filtering, dedupe."""

import copy
import hashlib
import hmac
import threading

import pytest

from prm.github.webhook import Deduper, PREvent, classify_pr_event, parse_pr_event, verify_signature

SECRET = b"test-webhook-key"
HEAD = "a" * 40
BASE = "b" * 40


def sign(body: bytes, key: bytes = SECRET) -> str:
    return "sha256=" + hmac.new(key, body, hashlib.sha256).hexdigest()


def pr_payload(action: str = "opened", **pr_overrides) -> dict:
    pr = {
        "number": 7,
        "draft": False,
        "head": {"sha": HEAD, "repo": {"full_name": "acme/widgets"}},
        "base": {"sha": BASE, "repo": {"full_name": "acme/widgets"}},
    }
    pr.update(pr_overrides)
    return {"action": action, "pull_request": pr, "repository": {"full_name": "acme/widgets"}}


# ------------------------------------------------------------------------------------------- signature


def test_valid_signature_verifies():
    body = b'{"zen": "hi"}'
    assert verify_signature(SECRET, body, sign(body))
    assert verify_signature(SECRET, body, sign(body).upper().replace("SHA256=", "sha256="))


@pytest.mark.parametrize(
    "header",
    [None, "", "sha256=", "sha1=" + "0" * 40, "sha256=" + "0" * 64, "sha256=zz" + "0" * 62, "sha256=ü" * 3],
)
def test_missing_or_malformed_signature_fails(header):
    assert not verify_signature(SECRET, b"{}", header)


def test_signature_with_other_key_or_tampered_body_fails():
    body = b'{"a": 1}'
    assert not verify_signature(SECRET, body, sign(body, b"other-key"))
    assert not verify_signature(SECRET, body + b" ", sign(body))


def test_empty_secret_never_verifies():
    body = b"{}"
    assert not verify_signature(b"", body, sign(body, b""))


# ------------------------------------------------------------------------------------------- filtering


@pytest.mark.parametrize("action", ["opened", "synchronize", "reopened"])
def test_handled_actions_parse(action):
    expected = PREvent("acme/widgets", 7, HEAD, BASE, action)
    assert parse_pr_event("pull_request", pr_payload(action)) == expected


@pytest.mark.parametrize("action", ["closed", "edited", "labeled", "ready_for_review", None])
def test_other_actions_are_ignored(action):
    assert parse_pr_event("pull_request", pr_payload(action)) is None
    assert classify_pr_event("pull_request", pr_payload(action)) == "action not handled"


@pytest.mark.parametrize("event", ["push", "issues", "ping", ""])
def test_other_events_are_ignored(event):
    assert classify_pr_event(event, pr_payload()) == "event not handled"


def test_drafts_are_ignored():
    assert classify_pr_event("pull_request", pr_payload(draft=True)) == "draft pull request"


def test_forks_are_ignored():
    fork = pr_payload(head={"sha": HEAD, "repo": {"full_name": "mallory/widgets"}})
    assert classify_pr_event("pull_request", fork) == "fork pull request"
    deleted_fork = pr_payload(head={"sha": HEAD, "repo": None})
    assert classify_pr_event("pull_request", deleted_fork) == "fork pull request"


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.pop("pull_request"),
        lambda p: p.update(repository={"full_name": "../etc"}),
        lambda p: p.update(repository={"full_name": "acme/.."}),
        lambda p: p["pull_request"].update(number="7"),
        lambda p: p["pull_request"].update(number=True),
        lambda p: p["pull_request"].update(number=0),
        lambda p: p["pull_request"]["head"].update(sha="HEAD; rm -rf /"),
        lambda p: p["pull_request"]["base"].update(sha=None),
    ],
)
def test_malformed_payloads_are_ignored(mutate):
    payload = copy.deepcopy(pr_payload())
    mutate(payload)
    assert parse_pr_event("pull_request", payload) is None


def test_non_dict_payload_is_ignored():
    assert classify_pr_event("pull_request", ["not", "a", "dict"]) == "action not handled"


# ------------------------------------------------------------------------------------------------ dedupe


def test_deduper_drops_repeats_and_forgets():
    d = Deduper()
    assert d.first_time("acme/widgets", 7, HEAD)
    assert not d.first_time("acme/widgets", 7, HEAD)
    assert not d.first_time("ACME/Widgets", 7, HEAD)  # GitHub names are case-insensitive
    assert d.first_time("acme/widgets", 7, BASE)
    assert d.first_time("acme/widgets", 8, HEAD)
    d.forget("acme/widgets", 7, HEAD)
    assert d.first_time("acme/widgets", 7, HEAD)


def test_deduper_is_bounded_and_evicts_oldest():
    d = Deduper(maxsize=3)
    for pr in range(5):
        assert d.first_time("acme/widgets", pr, HEAD)
    assert len(d) == 3
    assert d.first_time("acme/widgets", 0, HEAD)  # evicted, so new again
    assert not d.first_time("acme/widgets", 4, HEAD)


def test_deduper_rejects_bad_size():
    with pytest.raises(ValueError):
        Deduper(maxsize=0)


def test_deduper_is_thread_safe():
    d = Deduper()
    wins: list[bool] = []
    lock = threading.Lock()

    def hit() -> None:
        first = d.first_time("acme/widgets", 1, HEAD)
        with lock:
            wins.append(first)

    threads = [threading.Thread(target=hit) for _ in range(32)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert wins.count(True) == 1
