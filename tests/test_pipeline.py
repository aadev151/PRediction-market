"""Pipeline (PLAN §6.13): a fake GitHub client recording statuses and the fake traders through the REAL
MarketEngine on an in-memory DB. GitHub markets stop at GATED (post-merge settlement isn't wired yet). Fail
closed: every error path ends in VOID + a failure status."""

import random
import subprocess
from pathlib import Path

import pytest

from prm import pipeline
from prm.config import RuntimeEnv, ServerConfig, TraderSpec
from prm.db import make_session_factory
from prm.github.client import PullRequest
from prm.market.engine import MarketEngine
from prm.models import Event, Market
from prm.pipeline import FACTORY_ERROR, PipelineDeps, run_local_market, run_pr_market
from prm.traders.fake import FakeProvider
from prm.traders.roster import EngineProvider
from prm.types import BeliefResult, PRBundle
from tests.traders._bundles import BUG_DIFF, GOOD_DIFF

REPO = "acme/widgets"
HEAD, BASE = "h" * 40, "b" * 40
URL = "http://prm.test"
TOML = '[target]\nname = "widgets"\nholdouts_repo = "acme/hidden"\n[settle]\ncommand = "run {holdouts}"\n'
ROOT = Path(__file__).resolve().parents[1]
SAMPLE = ROOT / "examples" / "sample-target"
PATCHES = ROOT / "examples" / "patches"


class FakeGitHub:
    def __init__(self, diff=GOOD_DIFF, toml=TOML, head=HEAD, fail_post_on=None, fail_diff=False):
        self.diff, self.toml, self.head = diff, toml, head
        self.fail_post_on, self.fail_diff = fail_post_on, fail_diff
        self.statuses: list[tuple[str, str, str | None]] = []

    async def post_status(self, repo, sha, state, description, target_url=None, context="pr-market"):
        assert (repo, sha, context) == (REPO, HEAD, "pr-market")
        if self.fail_post_on and self.fail_post_on in description:
            self.fail_post_on = None  # fail once
            raise RuntimeError("github down")
        self.statuses.append((state, description, target_url))

    async def get_pr(self, repo, number):
        return PullRequest(number, "Title", "Body", self.head, BASE, REPO, REPO, False, ())

    async def get_diff(self, repo, number):
        if self.fail_diff:
            raise RuntimeError("boom https://secret.example/?token=x")
        return self.diff

    async def get_file(self, repo, path, ref):
        assert (path, ref) == (".prm.toml", BASE)  # always the BASE sha (R-TGT-3)
        return self.toml

    async def download_tree(self, repo, sha, dest):
        dest.mkdir(parents=True)
        (dest / "README.md").write_text(sha)
        return dest


class Fixed:
    """Every trader believes ``p``: lands the price in the escalation band."""

    def __init__(self, p):
        self.p = p

    async def get_belief(self, spec: TraderSpec, bundle: PRBundle, escalation=None) -> BeliefResult:
        return BeliefResult(spec.name, self.p, "why", [], spec.model, 1, 1, 1, 0.0, None)


def make_deps(tmp_path, gh=None, provider=None):
    cfg = ServerConfig()
    sf = make_session_factory("sqlite://")
    llm = provider or EngineProvider(FakeProvider())
    engine = MarketEngine(sf, cfg, llm, rng=random.Random(3))  # noqa: S311 - trade order only
    env = RuntimeEnv(cache_dir=tmp_path / "cache", public_url=URL)
    return PipelineDeps(env, cfg, sf, engine, gh)


def market(deps, mid) -> Market:
    with deps.session_factory() as s:
        return s.get(Market, mid)


async def test_good_pr_auto_merges_and_stays_gated(tmp_path):
    gh = FakeGitHub(GOOD_DIFF)
    deps = make_deps(tmp_path, gh)
    mid = await run_pr_market(REPO, 7, HEAD, deps=deps)
    assert [s for s, _, _ in gh.statuses] == ["pending", "success"]
    assert gh.statuses[0][1] == "market open…"
    assert gh.statuses[1][1].startswith("auto-merge OK (p=")
    assert gh.statuses[1][2] == f"{URL}/m/{mid}"
    m = market(deps, mid)
    assert (m.state, m.source, m.pr_number, m.head_sha, m.base_sha) == ("GATED", "github", 7, HEAD, BASE)
    assert not list((tmp_path / "cache").iterdir())  # temp trees cleaned up


async def test_bug_pr_blocked_keeps_its_gate_text(tmp_path):
    gh = FakeGitHub(BUG_DIFF)
    deps = make_deps(tmp_path, gh)
    mid = await run_pr_market(REPO, 7, HEAD, deps=deps)
    assert [s for s, _, _ in gh.statuses] == ["pending", "failure"]
    assert gh.statuses[1][1].startswith("blocked (p=")
    m = market(deps, mid)
    assert (m.state, m.gate_state, m.resolution) == ("GATED", "failure", None)


async def test_escalation_posts_pending_then_no_consensus(tmp_path):
    gh = FakeGitHub(GOOD_DIFF)
    deps = make_deps(tmp_path, gh, provider=Fixed(0.75))
    await run_pr_market(REPO, 7, HEAD, deps=deps)
    descs = [d for _, d, _ in gh.statuses]
    assert descs[1].startswith("escalation 1/2 (p=") and descs[2].startswith("escalation 2/2 (p=")
    assert gh.statuses[3][0] == "failure" and gh.statuses[3][1].startswith("no consensus")


async def test_missing_prm_toml_posts_error_and_opens_no_market(tmp_path):
    gh = FakeGitHub(toml=None)
    deps = make_deps(tmp_path, gh)
    assert await run_pr_market(REPO, 7, HEAD, deps=deps) is None
    assert gh.statuses == [("pending", "market open…", None), ("error", "no .prm.toml on base branch", None)]


async def test_remote_config_with_local_path_is_invalid(tmp_path):
    bad = TOML.replace('holdouts_repo = "acme/hidden"', 'holdouts_path = "/etc"')
    gh = FakeGitHub(toml=bad)
    deps = make_deps(tmp_path, gh)
    assert await run_pr_market(REPO, 7, HEAD, deps=deps) is None
    assert gh.statuses[-1] == ("failure", "invalid .prm.toml on base branch", None)


async def test_head_moved_posts_error(tmp_path):
    gh = FakeGitHub(head="c" * 40)
    deps = make_deps(tmp_path, gh)
    assert await run_pr_market(REPO, 7, HEAD, deps=deps) is None
    assert gh.statuses[-1][0] == "error"


async def test_error_before_market_posts_failure_and_logs_type_only(tmp_path, caplog):
    gh = FakeGitHub(fail_diff=True)
    deps = make_deps(tmp_path, gh)
    assert await run_pr_market(REPO, 7, HEAD, deps=deps) is None
    assert gh.statuses[-1] == ("failure", FACTORY_ERROR, None)
    assert "RuntimeError" in caplog.text and "secret.example" not in caplog.text


async def test_exception_after_create_voids_with_public_safe_reason(tmp_path):
    gh = FakeGitHub(GOOD_DIFF, fail_post_on="auto-merge OK")
    deps = make_deps(tmp_path, gh)
    mid = await run_pr_market(REPO, 7, HEAD, deps=deps)
    assert gh.statuses[-1] == ("failure", FACTORY_ERROR, f"{URL}/m/{mid}")
    assert market(deps, mid).state == "VOID"
    with deps.session_factory() as s:
        details = [e.detail for e in s.query(Event).filter(Event.market_id == mid)]
    assert "factory error: RuntimeError" in details  # the type only, never the message
    assert not any("github down" in (d or "") for d in details)


async def test_never_raises_even_without_deps(monkeypatch):
    def broken():
        raise RuntimeError("no config")

    monkeypatch.setattr(pipeline, "default_deps", broken)
    assert await run_pr_market(REPO, 7, HEAD) is None


async def test_rerun_same_sha_reuses_market_and_reposts_gate(tmp_path):
    gh = FakeGitHub(BUG_DIFF)
    deps = make_deps(tmp_path, gh)
    first = await run_pr_market(REPO, 7, HEAD, deps=deps)
    final = gh.statuses[-1]
    assert await run_pr_market(REPO, 7, HEAD, deps=deps) == first
    assert gh.statuses[-1] == final


async def test_local_demo_settlement_error_voids(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("disk")

    monkeypatch.setattr(pipeline, "settle_postmerge", broken)
    deps = make_deps(tmp_path)
    statuses = []
    mid = await run_local_market(
        SAMPLE, PATCHES / "good_docstring.patch", repo="local/s", deps=deps, statuses=statuses
    )
    assert market(deps, mid).state == "VOID"
    assert statuses[-1][:2] == ("failure", FACTORY_ERROR)


async def test_local_demo_rejects_unknown_after_merge(tmp_path):
    with pytest.raises(ValueError):
        await run_local_market(
            SAMPLE,
            PATCHES / "good_docstring.patch",
            repo="local/s",
            after_merge="merge",
            deps=make_deps(tmp_path),
        )


def test_apply_patch_inside_an_enclosing_git_repo(tmp_path):
    """The cache dir may live inside a checkout; git must still patch the copied tree, not skip it."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    tree = tmp_path / "cache" / "work" / "head"
    tree.mkdir(parents=True)
    (tree / "a.txt").write_text("one\n")
    patch = tmp_path / "p.patch"
    patch.write_text("--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-one\n+two\n")
    pipeline.apply_patch(tree, patch)
    assert (tree / "a.txt").read_text() == "two\n"


def test_apply_patch_failure_is_a_clear_error(tmp_path):
    tree = tmp_path / "t"
    tree.mkdir()
    (tree / "a.txt").write_text("other\n")
    patch = tmp_path / "p.patch"
    patch.write_text("--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-one\n+two\n")
    with pytest.raises(pipeline.PatchError, match="p.patch"):
        pipeline.apply_patch(tree, patch)
