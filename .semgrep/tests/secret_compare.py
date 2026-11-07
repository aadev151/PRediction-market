# ruff: noqa
# Rule test fixture for prm-secret-compare-not-constant-time (semgrep --test). Not shipped code.
import hashlib
import hmac


def check(token, expected_token, signature, body, key, webhook_secret, provided):
    # ruleid: prm-secret-compare-not-constant-time
    if token == expected_token:
        pass
    # ruleid: prm-secret-compare-not-constant-time
    if signature != "sha256=" + hmac.new(key, body, hashlib.sha256).hexdigest():
        pass
    # ruleid: prm-secret-compare-not-constant-time
    if provided == webhook_secret:
        pass
    # ruleid: prm-secret-compare-not-constant-time
    if hashlib.sha256(body).hexdigest() == provided:
        pass
    # ok: prm-secret-compare-not-constant-time
    if hmac.compare_digest(token, expected_token):
        pass
    # ok: prm-secret-compare-not-constant-time
    if token == None:
        pass
    # ok: prm-secret-compare-not-constant-time
    if webhook_secret == "":
        pass
    # ok: prm-secret-compare-not-constant-time
    if len(signature) != 71:
        pass
    # ok: prm-secret-compare-not-constant-time
    if provided == body:
        pass
