"""Pipeline (PLAN §6.13): a fake GitHub client recording statuses, fake settlers, the fake traders through the
REAL MarketEngine on an in-memory DB. Fail closed: every error path ends in VOID + a failure status."""

import random
import subprocess
import sys
import types
from pathlib import Path

import pytest

from prm import pipeline
from prm.config import RuntimeEnv, ServerConfig, TraderSpec, load_target_config
from prm.db import make_session_factory
from prm.github.client import PullRequest
from prm.market.engine import MarketEngine
from prm.models import Market
from prm.pipeline import FACTORY_ERROR, PipelineDeps, run_pr_market
from prm.traders.fake import FakeProvider
from prm.traders.roster import EngineProvider
from prm.types import BeliefResult, PRBundle, SettlementResult
from tests.traders._bundles import BUG_DIFF, GOOD_DIFF

REPO = "acme/widgets"
HEAD, BASE = "h" * 40, "b" * 40
URL = "http://prm.test"
TOML = '[target]\nname = "widgets"\nholdouts_repo = "acme/hidden"\n[settle]\ncommand = "run {holdouts}"\n'
YES = SettlementResult("YES", 12, 12, [], 5, None)
NO = SettlementResult("NO", 10, 12, [], 5, None)


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


class Settler:
    def __init__(self, result=YES, exc=None):
        self.result, self.exc, self.calls = result, exc, []

    def __call__(self, head_dir: Path, target):
        self.calls.append(head_dir)
        assert (head_dir / "README.md").read_text() == HEAD  # settles the HEAD tree
        if self.exc:
            raise self.exc
        return self.result


def make_deps(tmp_path, gh, settler, provider=None, cfg=None):
    cfg = cfg or ServerConfig()
    sf = make_session_factory("sqlite://")
    llm = provider or EngineProvider(FakeProvider())
    engine = MarketEngine(sf, cfg, llm, rng=random.Random(3))  # noqa: S311 - trade order only
    env = RuntimeEnv(cache_dir=tmp_path / "cache", public_url=URL)
    return PipelineDeps(env, cfg, sf, engine, gh, settler)


def market(deps, mid) -> Market:
    with deps.session_factory() as s:
        return s.get(Market, mid)


async def test_good_pr_auto_merges_and_settles_yes(tmp_path):
    gh, settler = FakeGitHub(GOOD_DIFF), Settler(YES)
    deps = make_deps(tmp_path, gh, settler)
    mid = await run_pr_market(REPO, 7, HEAD, deps=deps)
    states = [s for s, _, _ in gh.statuses]
    assert states == ["pending", "success", "success"]
    assert gh.statuses[0][1] == "market open…"
    assert gh.statuses[1][1].startswith("auto-merge OK (p=")
    assert gh.statuses[2][1].endswith("· settled YES · market right")
    assert gh.statuses[2][2] == f"{URL}/m/{mid}"
    m = market(deps, mid)
    assert (m.state, m.source, m.pr_number, m.head_sha, m.base_sha) == ("SETTLED", "github", 7, HEAD, BASE)
    assert len(settler.calls) == 1
    assert not list((tmp_path / "cache").iterdir())  # temp trees cleaned up


async def test_bug_pr_blocked_then_refunded(tmp_path):
    gh = FakeGitHub(BUG_DIFF)
    deps = make_deps(tmp_path, gh, Settler(NO))
    mid = await run_pr_market(REPO, 7, HEAD, deps=deps)
    assert [s for s, _, _ in gh.statuses] == ["pending", "failure", "failure"]
    assert gh.statuses[1][1].startswith("blocked (p=")
    assert gh.statuses[2][1].endswith("· refunded · holdouts agree")
    m = market(deps, mid)
    assert (m.state, m.resolution, m.outcome) == ("VOID", "refunded", "NO")


async def test_escalation_posts_pending_then_no_consensus(tmp_path):
    gh = FakeGitHub(GOOD_DIFF)
    deps = make_deps(tmp_path, gh, Settler(YES), provider=Fixed(0.75))
    await run_pr_market(REPO, 7, HEAD, deps=deps)
    descs = [d for _, d, _ in gh.statuses]
    assert descs[1].startswith("escalation 1/2 (p=") and descs[2].startswith("escalation 2/2 (p=")
    assert gh.statuses[3][0] == "failure" and gh.statuses[3][1].startswith("no consensus")


async def test_missing_prm_toml_posts_error_and_opens_no_market(tmp_path):
    gh = FakeGitHub(toml=None)
    deps = make_deps(tmp_path, gh, Settler())
    assert await run_pr_market(REPO, 7, HEAD, deps=deps) is None
    assert gh.statuses == [("pending", "market open…", None), ("error", "no .prm.toml on base branch", None)]


async def test_remote_config_with_local_path_is_invalid(tmp_path):
    bad = TOML.replace('holdouts_repo = "acme/hidden"', 'holdouts_path = "/etc"')
    gh = FakeGitHub(toml=bad)
    deps = make_deps(tmp_path, gh, Settler())
    assert await run_pr_market(REPO, 7, HEAD, deps=deps) is None
    assert gh.statuses[-1] == ("failure", "invalid .prm.toml on base branch", None)


async def test_head_moved_posts_error(tmp_path):
    gh = FakeGitHub(head="c" * 40)
    deps = make_deps(tmp_path, gh, Settler())
    assert await run_pr_market(REPO, 7, HEAD, deps=deps) is None
    assert gh.statuses[-1][0] == "error"


async def test_error_before_market_posts_failure_and_logs_type_only(tmp_path, caplog):
    gh = FakeGitHub(fail_diff=True)
    deps = make_deps(tmp_path, gh, Settler())
    assert await run_pr_market(REPO, 7, HEAD, deps=deps) is None
    assert gh.statuses[-1] == ("failure", FACTORY_ERROR, None)
    assert "RuntimeError" in caplog.text and "secret.example" not in caplog.text


async def test_exception_after_create_voids_and_posts_failure(tmp_path):
    gh = FakeGitHub(GOOD_DIFF, fail_post_on="auto-merge OK")
    deps = make_deps(tmp_path, gh, Settler(YES))
    mid = await run_pr_market(REPO, 7, HEAD, deps=deps)
    assert gh.statuses[-1] == ("failure", FACTORY_ERROR, f"{URL}/m/{mid}")
    assert market(deps, mid).state == "VOID"


async def test_raising_settler_voids(tmp_path):
    gh = FakeGitHub(GOOD_DIFF)
    deps = make_deps(tmp_path, gh, Settler(exc=OSError("disk")))
    mid = await run_pr_market(REPO, 7, HEAD, deps=deps)
    assert gh.statuses[-1][:2] == ("failure", FACTORY_ERROR)
    assert market(deps, mid).state == "VOID"


async def test_settlement_infra_error_voids_and_flips_status_to_failure(tmp_path):
    gh = FakeGitHub(GOOD_DIFF)
    deps = make_deps(tmp_path, gh, Settler(SettlementResult.infra_error("settlement not available")))
    mid = await run_pr_market(REPO, 7, HEAD, deps=deps)
    assert gh.statuses[1][0] == "success"
    assert gh.statuses[-1][:2] == ("failure", "market void: human review required")
    assert (market(deps, mid).state, market(deps, mid).resolution) == ("VOID", "void")


async def test_remote_driver_stays_gated(tmp_path):
    gh = FakeGitHub(GOOD_DIFF, toml=TOML.replace("[settle]\n", '[settle]\ndriver = "remote"\n'))
    settler = Settler()
    deps = make_deps(tmp_path, gh, settler)
    mid = await run_pr_market(REPO, 7, HEAD, deps=deps)
    assert market(deps, mid).state == "GATED" and settler.calls == []
    assert [s for s, _, _ in gh.statuses] == ["pending", "success"]


async def test_rerun_same_sha_reuses_market_and_reposts_final(tmp_path):
    gh = FakeGitHub(GOOD_DIFF)
    settler = Settler(YES)
    deps = make_deps(tmp_path, gh, settler)
    first = await run_pr_market(REPO, 7, HEAD, deps=deps)
    final = gh.statuses[-1]
    assert await run_pr_market(REPO, 7, HEAD, deps=deps) == first
    assert gh.statuses[-1] == final and len(settler.calls) == 1


def test_default_settler_without_wp_d_is_an_infra_error(monkeypatch, tmp_path):
    monkeypatch.setitem(sys.modules, "prm.settle.local", types.ModuleType("prm.settle.local"))
    target = load_target_config_text(tmp_path)
    result = pipeline.default_settler(tmp_path, target)
    assert result.outcome is None and result.error == "settlement not available"


def test_default_settler_delegates_to_settle_tree(monkeypatch, tmp_path):
    fake = types.ModuleType("prm.settle.local")
    fake.settle_tree = lambda head, target: YES
    monkeypatch.setitem(sys.modules, "prm.settle.local", fake)
    assert pipeline.default_settler(tmp_path, load_target_config_text(tmp_path)) is YES


def load_target_config_text(tmp_path):
    (tmp_path / ".prm.toml").write_text(
        '[target]\nname = "t"\nholdouts_path = "../h"\n[settle]\ncommand = "x"\n'
    )
    return load_target_config(tmp_path)


def test_absolute_holdouts_resolves_against_the_target_root(tmp_path):
    target = load_target_config_text(tmp_path)
    resolved = pipeline.absolute_holdouts(target, tmp_path)
    assert resolved.target.holdouts_path == str((tmp_path.parent / "h").resolve())
    assert Path(resolved.target.holdouts_path).is_absolute()


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
    (tmp_path / "a.txt").write_text("other\n")
    patch = tmp_path / "p.patch"
    patch.write_text("--- a/a.txt\n+++ b/a.txt\n@@ -1 +1 @@\n-one\n+two\n")
    with pytest.raises(pipeline.PatchError, match="p.patch"):
        pipeline.apply_patch(tmp_path, patch)
