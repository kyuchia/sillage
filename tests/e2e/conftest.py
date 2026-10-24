"""
Playwright end-to-end tests for the static demo (docs/, as served by GitHub Pages).

They are skipped unless a target is given, so a plain `pytest` (and the CI pytest job)
never needs a browser:

    # the live demo
    pytest tests/e2e --base-url https://kyuchia.github.io/sillage/ --browser chromium

    # this checkout's docs/, served locally (what CI does)
    python -m http.server 8765 --directory docs &
    pytest tests/e2e --base-url http://localhost:8765/ \
        --browser chromium --browser firefox --browser webkit

Every test runs at two viewports: desktop (1280×800) and phone (390×844). WebGL output
is not compared pixel by pixel; the tests check that elements exist and respond, that
nothing reaches the console as an error, and that every request succeeds.

One request is meant to fail: the page asks `/api/range` first and treats a 404 as "no
API here, play the baked scenes" (boot() in docs/index.html). Chromium and WebKit log
that 404 to the console, so it is the single allowed exception in both checks below.
"""

from dataclasses import dataclass, field
from urllib.parse import urljoin, urlparse

import pytest
from playwright.sync_api import expect

E2E_DIR = __file__.rsplit("/", 1)[0]

VIEWPORTS = {
    "desktop": {"width": 1280, "height": 800},
    "phone": {"width": 390, "height": 844},
}

# A scene is ready once init() has built the legend and the status line carries no
# warn/error level. The 5 MB scene JSON can take a while on a slow link, and on a
# machine without a GPU (CI runners) every frame is drawn in software.
READY_TIMEOUT_MS = 90_000
# Poll on a timer, not on requestAnimationFrame: the replay redraws every frame, and in
# software WebGL rAF-based polling queues behind those frames for tens of seconds.
POLL_MS = 250
# Clicks and assertions wait on the same busy main thread.
ACTION_TIMEOUT_MS = 30_000
expect.set_options(timeout=ACTION_TIMEOUT_MS)

# Requests the browser itself cancelled (MapLibre drops tiles it no longer needs when
# the camera moves). These are not failures of the request.
CANCELLED = ("ERR_ABORTED", "NS_BINDING_ABORTED", "cancelled", "Load request cancelled")


def pytest_collection_modifyitems(config, items):
    if config.getoption("base_url", default=None):
        return
    skip = pytest.mark.skip(reason="e2e: pass --base-url (live demo or a local server over docs/)")
    for item in items:
        if str(item.fspath).startswith(E2E_DIR):
            item.add_marker(skip)


def is_expected_api_404(url: str, status: int | None = None) -> bool:
    return urlparse(url).path == "/api/range" and status in (None, 404)


@dataclass
class Demo:
    page: object
    viewport: str
    console_errors: list = field(default_factory=list)
    page_errors: list = field(default_factory=list)
    bad_responses: list = field(default_factory=list)
    failed_requests: list = field(default_factory=list)
    responses: list = field(default_factory=list)
    scene: dict | None = None

    @property
    def phone(self) -> bool:
        return self.viewport == "phone"

    def wait_ready(self, scene_label: str | None = None):
        page = self.page
        page.locator("#modeList .mode").first.wait_for(state="attached", timeout=READY_TIMEOUT_MS)
        page.wait_for_function(
            "() => !document.getElementById('dial').dataset.level",
            polling=POLL_MS,
            timeout=READY_TIMEOUT_MS,
        )
        if scene_label:
            page.wait_for_function(
                "label => document.getElementById('banner').textContent.includes(label)",
                arg=scene_label,
                polling=POLL_MS,
                timeout=READY_TIMEOUT_MS,
            )
        # Let the basemap tiles and the deck.gl overlay settle before anything is judged.
        page.wait_for_load_state("networkidle", timeout=READY_TIMEOUT_MS)


@pytest.fixture(params=list(VIEWPORTS), ids=list(VIEWPORTS))
def viewport(request):
    return request.param


@pytest.fixture
def manifest(page, base_url):
    """docs/scenes/manifest.json as served."""
    res = page.request.get(urljoin(base_url, "scenes/manifest.json"))
    assert res.ok, f"manifest: {res.status}"
    return res.json()


def _open(page, base_url, viewport, query=""):
    """Load the demo at a viewport, recording console and network from the first byte."""
    d = Demo(page=page, viewport=viewport)

    def on_console(msg):
        if msg.type != "error":
            return
        if is_expected_api_404(msg.location.get("url", "")):
            return
        d.console_errors.append(f"{msg.text}  @ {msg.location.get('url', '')}")

    def on_response(res):
        d.responses.append((res.url, res.status))
        if res.status >= 400 and not is_expected_api_404(res.url, res.status):
            d.bad_responses.append(f"{res.status} {res.url}")

    def on_failed(req):
        err = req.failure or ""
        if any(c in err for c in CANCELLED):
            return
        d.failed_requests.append(f"{err} {req.url}")

    page.on("console", on_console)
    page.on("pageerror", lambda exc: d.page_errors.append(str(exc)))
    page.on("response", on_response)
    page.on("requestfailed", on_failed)

    page.set_default_timeout(ACTION_TIMEOUT_MS)
    page.set_viewport_size(VIEWPORTS[viewport])
    page.goto(urljoin(base_url, query))
    d.wait_ready()
    return d


@pytest.fixture
def demo(page, base_url, viewport):
    """The demo as a visitor first sees it: the default (first, and heaviest) scene."""
    return _open(page, base_url, viewport)


@pytest.fixture
def light_demo(page, base_url, viewport, manifest):
    """The demo on its lightest scene, for tests that click.

    The replay redraws every trail on every frame. Without a GPU (this sandbox, CI
    runners) the default scene runs at well under 1 frame per second, and Playwright's
    clicks wait for the page to be stable across frames. The controls are the same
    code whichever scene is loaded, so the clicking tests use the lightest one.
    """
    lightest = min(manifest["scenes"], key=lambda sc: sc.get("points", float("inf")))
    d = _open(page, base_url, viewport, "?scene=" + lightest["slug"])
    d.scene = lightest
    return d
