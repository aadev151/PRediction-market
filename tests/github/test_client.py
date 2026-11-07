"""GitHubClient against httpx.MockTransport (PLAN §6.11): no network, no real token."""

import io
import json
import logging
import tarfile

import httpx
import pytest

from prm.github.client import GitHubClient, GitHubError, PullRequest, UnsafeArchive, safe_extract_tarball

TOKEN = "ghp_test_not_a_real_token_123"
PR_JSON = {
    "number": 7,
    "title": "Fix limits",
    "body": None,
    "draft": False,
    "head": {"sha": "h" * 40, "repo": {"full_name": "fork/widgets"}},
    "base": {"sha": "b" * 40, "repo": {"full_name": "acme/widgets"}},
    "labels": [{"name": "prm:auto-merge"}, {"name": "bug"}],
}


def _client(handler, token=TOKEN):
    return GitHubClient(token, transport=httpx.MockTransport(handler))


def _tar(entries) -> bytes:
    """entries: list of (name, kind, payload) with kind in file/dir/sym/lnk/fifo."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, kind, payload in entries:
            info = tarfile.TarInfo(name)
            if kind == "file":
                data = payload.encode()
                info.size = len(data)
                info.mode = 0o644
                tar.addfile(info, io.BytesIO(data))
                continue
            info.type = {
                "dir": tarfile.DIRTYPE,
                "sym": tarfile.SYMTYPE,
                "lnk": tarfile.LNKTYPE,
                "fifo": tarfile.FIFOTYPE,
            }[kind]
            info.linkname = payload or ""
            tar.addfile(info)
    return buf.getvalue()


async def test_get_pr_parses_fields_and_sends_token():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["auth"] = request.headers.get("authorization")
        seen["path"] = request.url.path
        return httpx.Response(200, json=PR_JSON)

    pr = await _client(handler).get_pr("acme/widgets", 7)
    assert pr == PullRequest(
        7, "Fix limits", "", "h" * 40, "b" * 40, "fork/widgets", "acme/widgets", False,
        ("prm:auto-merge", "bug"),
    )  # fmt: skip
    assert seen == {"auth": f"Bearer {TOKEN}", "path": "/repos/acme/widgets/pulls/7"}


async def test_token_read_from_env_at_call_time(monkeypatch):
    seen = []

    def handler(request):
        seen.append(request.headers.get("authorization"))
        return httpx.Response(200, json=PR_JSON)

    client = _client(handler, token=None)
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    await client.get_pr("acme/widgets", 7)
    monkeypatch.setenv("GITHUB_TOKEN", "env-token-value")
    await client.get_pr("acme/widgets", 7)
    assert seen == [None, "Bearer env-token-value"]


async def test_get_diff_uses_diff_media_type():
    def handler(request):
        assert request.headers["accept"] == "application/vnd.github.diff"
        return httpx.Response(200, text="diff --git a/x b/x\n")

    assert await _client(handler).get_diff("acme/widgets", 7) == "diff --git a/x b/x\n"


async def test_get_file_at_ref_and_404_is_none():
    def handler(request):
        assert request.url.params["ref"] == "b" * 40
        if request.url.path.endswith("/contents/.prm.toml"):
            assert request.headers["accept"] == "application/vnd.github.raw"
            return httpx.Response(200, text="[target]\n")
        return httpx.Response(404, json={"message": "Not Found"})

    client = _client(handler)
    assert await client.get_file("acme/widgets", ".prm.toml", "b" * 40) == "[target]\n"
    assert await client.get_file("acme/widgets", "missing.toml", "b" * 40) is None


@pytest.mark.parametrize("path", ["/etc/passwd", "../x", "a/../../x", ""])
async def test_get_file_rejects_escaping_paths(path):
    with pytest.raises(ValueError):
        await _client(lambda r: httpx.Response(200)).get_file("acme/widgets", path, "main")


@pytest.mark.parametrize("repo", ["acme", "acme/../x", "../widgets", "a/b/c", "acme/w idgets"])
async def test_bad_repo_names_rejected(repo):
    with pytest.raises(ValueError):
        await _client(lambda r: httpx.Response(200, json=PR_JSON)).get_pr(repo, 1)


async def test_get_labels():
    def handler(request):
        assert request.url.path == "/repos/acme/widgets/issues/7/labels"
        return httpx.Response(200, json=[{"name": "a"}, {"name": "b"}])

    assert await _client(handler).get_labels("acme/widgets", 7) == ["a", "b"]


async def test_post_status_truncates_and_sets_context():
    bodies = []

    def handler(request):
        assert request.method == "POST" and request.url.path == f"/repos/acme/widgets/statuses/{'h' * 40}"
        bodies.append(json.loads(request.content))
        return httpx.Response(201, json={})

    client = _client(handler)
    await client.post_status("acme/widgets", "h" * 40, "success", "x" * 300, "http://prm/m/1")
    await client.post_status("acme/widgets", "h" * 40, "pending", "market open")
    assert bodies[0] == {
        "state": "success", "description": "x" * 140, "context": "pr-market", "target_url": "http://prm/m/1",
    }  # fmt: skip
    assert bodies[1] == {"state": "pending", "description": "market open", "context": "pr-market"}


async def test_post_status_rejects_unknown_state():
    with pytest.raises(ValueError):
        await _client(lambda r: httpx.Response(201)).post_status("acme/widgets", "abc", "merged", "x")


async def test_merge_pr_squash():
    def handler(request):
        assert request.method == "PUT"
        assert json.loads(request.content) == {"merge_method": "squash", "sha": "abc"}
        return httpx.Response(200, json={"merged": True})

    assert await _client(handler).merge_pr("acme/widgets", 7, "abc") is True


async def test_http_error_carries_no_token_and_token_never_logged(caplog):
    caplog.set_level(logging.DEBUG)
    client = _client(lambda r: httpx.Response(500, text=f"echo {r.headers.get('authorization')}"))
    with pytest.raises(GitHubError) as exc:
        await client.get_pr("acme/widgets", 7)
    assert "HTTP 500" in str(exc.value)
    assert TOKEN not in str(exc.value)
    assert TOKEN not in repr(client) and TOKEN not in str(client)
    assert TOKEN not in caplog.text


async def test_transport_error_becomes_github_error():
    def handler(request):
        raise httpx.ConnectError("boom", request=request)

    with pytest.raises(GitHubError, match="ConnectError"):
        await _client(handler).get_diff("acme/widgets", 1)


async def test_download_tree_strips_top_dir_and_follows_redirect(tmp_path):
    archive = _tar(
        [
            ("acme-widgets-abc/", "dir", None),
            ("acme-widgets-abc/README.md", "file", "hello"),
            ("acme-widgets-abc/src/app.py", "file", "print(1)"),
        ]
    )

    def handler(request):
        if request.url.host == "api.github.com":
            return httpx.Response(302, headers={"location": "https://codeload.github.com/acme/widgets/x"})
        assert "authorization" not in request.headers  # httpx drops it on a cross-origin redirect
        return httpx.Response(200, content=archive)

    dest = await _client(handler).download_tree("acme/widgets", "abc", tmp_path / "head")
    assert (dest / "README.md").read_text() == "hello"
    assert (dest / "src" / "app.py").read_text() == "print(1)"
    assert not (dest / "acme-widgets-abc").exists()


@pytest.mark.parametrize(
    "entry",
    [
        ("/etc/evil", "file", "x"),
        ("top/../../evil", "file", "x"),
        ("top/link", "sym", "/etc/passwd"),
        ("top/hard", "lnk", "top/README.md"),
        ("top/pipe", "fifo", None),
    ],
)
def test_safe_extract_rejects_unsafe_members(tmp_path, entry):
    archive = _tar([("top/README.md", "file", "ok"), entry])
    with pytest.raises(UnsafeArchive):
        safe_extract_tarball(archive, tmp_path / "out")
    assert not (tmp_path / "evil").exists()
    assert not (tmp_path / "out" / "README.md").exists()  # rejected before anything is written
