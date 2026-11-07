"""The dashboard's static pages (PLAN §6.14, R-UI-1): served at their routes, CSP-clean (no inline scripts,
scripts only from self or a pinned cdnjs version), every local reference resolves, no HTML sinks in the JS,
and the shared helpers' logic (prm.test.mjs) passes under node."""

import re
import shutil
import subprocess  # noqa: S404
from html.parser import HTMLParser
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from prm.config import RuntimeEnv, ServerConfig
from prm.server import create_app

STATIC = Path(__file__).resolve().parents[2] / "prm" / "dashboard" / "static"
# route -> file; each page PR adds its row
PAGES = {"/": "index.html", "/m/1": "market.html", "/how": "how.html"}
CDN_SCRIPT = re.compile(r"^https://cdnjs\.cloudflare\.com/ajax/libs/[\w.-]+/\d+\.\d+\.\d+/[\w./-]+\.js$")
FONTS = "https://fonts.googleapis.com/"


class Refs(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.scripts: list[dict[str, str | None]] = []
        self.inline_scripts = 0
        self.links: list[dict[str, str | None]] = []
        self.handlers: list[str] = []
        self._in_script = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        a = dict(attrs)
        self.handlers += [k for k in a if k.startswith("on")]
        if tag == "script":
            self.scripts.append(a)
            self._in_script = "src" not in a
        elif tag == "link":
            self.links.append(a)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_script = False

    def handle_data(self, data: str) -> None:
        if self._in_script and data.strip():
            self.inline_scripts += 1


def parse(name: str) -> Refs:
    refs = Refs()
    refs.feed((STATIC / name).read_text(encoding="utf-8"))
    return refs


@pytest.fixture
def client():
    env = RuntimeEnv(database_url_override="sqlite://")
    with TestClient(create_app(env, ServerConfig())) as c:
        yield c


@pytest.mark.parametrize("route,name", PAGES.items())
def test_each_page_is_served_at_its_route(client, route, name):
    res = client.get(route)
    assert res.status_code == 200
    assert res.text == (STATIC / name).read_text(encoding="utf-8")


def test_market_page_serves_for_any_id(client):
    if not (STATIC / "market.html").is_file():
        pytest.skip("market page not built yet")
    assert client.get("/m/123").text == (STATIC / "market.html").read_text(encoding="utf-8")


@pytest.mark.parametrize("name", sorted(p.name for p in STATIC.glob("*.html")))
def test_pages_obey_the_csp(name):
    refs = parse(name)
    assert refs.inline_scripts == 0, "inline <script> is blocked by script-src"
    assert refs.handlers == [], "inline event handlers are blocked by script-src"
    for s in refs.scripts:
        src = s["src"] or ""
        assert src.startswith("/static/") or CDN_SCRIPT.match(src), src
        if not src.startswith("/static/"):
            assert (s.get("integrity") or "").startswith("sha"), f"{src} needs subresource integrity"
            assert s.get("crossorigin") == "anonymous", src
    for link in refs.links:
        if link.get("rel") == "stylesheet":
            href = link["href"] or ""
            assert href.startswith("/static/") or href.startswith(FONTS), href


@pytest.mark.parametrize("name", sorted(p.name for p in STATIC.glob("*.html")))
def test_local_references_exist(client, name):
    refs = parse(name)
    paths = [s["src"] for s in refs.scripts] + [link["href"] for link in refs.links]
    for path in paths:
        if path and path.startswith("/static/"):
            assert client.get(path).status_code == 200, path


@pytest.mark.parametrize("js", sorted(p.name for p in STATIC.glob("*.js")))
def test_js_imports_resolve_and_avoid_html_sinks(js):
    text = (STATIC / js).read_text(encoding="utf-8")
    for sink in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
        assert sink not in text, f"{js} uses {sink}; build nodes with h()/textContent instead"
    for mod in re.findall(r'from\s+"(\./[\w.-]+\.js)"', text):
        assert (STATIC / mod).is_file(), f"{js} imports missing {mod}"


def test_how_page_links_only_tests_that_exist():
    """The invariants list on /how names real tests, and each link points at the file it names."""
    root = STATIC.parents[2]
    html = (STATIC / "how.html").read_text(encoding="utf-8")
    link = r'href="https://github\.com/[\w.-]+/[\w.-]+/blob/main/([\w/.]+)"><code>([\w/.]+)::(test_\w+)<'
    cited = re.findall(link, html)
    assert len(cited) >= 15
    for href_path, path, name in cited:
        assert href_path == path, (href_path, path)
        source = (root / path).read_text(encoding="utf-8")
        assert re.search(rf"^(async )?def {name}\(", source, re.M), f"{path}::{name}"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_shared_helpers_under_node():
    node = shutil.which("node")
    assert node is not None
    res = subprocess.run(  # noqa: S603
        [node, "--test", str(Path(__file__).with_name("prm.test.mjs"))],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert res.returncode == 0, res.stdout + res.stderr
