"""The browser adapter.

WHAT IS WORTH ATTACKING: the origin pin. A lens that can navigate anywhere is a
lens that can be steered anywhere, and the page under test is the thing doing
the steering -- it can redirect. So the tests below concentrate on URLs that
LOOK like the declared origin, and on the redirect that happens after the check.

The geometry tests need no browser at all: an intersection of two rectangles is
arithmetic, and testing it against a real page would test the browser instead of
the maths. The page-driving tests use a real Chromium against a `file://`-free
local server, and skip with a reason where the binary is absent -- failing
rather than skipping on the Linux CI legs, for the reason `sandbox_image` gives.
"""

from __future__ import annotations

import http.server
import os
import sys
import threading
from pathlib import Path

import pytest

from whetstone.errors import WhetstoneError
from whetstone.lenses.rendered_ui.browser import (
    Box,
    BrowserError,
    Origin,
    availability,
    rendered,
)


def _browser_is_expected() -> bool:
    """Linux CI must have a browser. Anywhere else it is optional.

    The workflow installs Chromium on the Linux legs, so a missing binary there
    is a broken pipeline rather than an environment nobody set up. Without this,
    every test below SKIPPED on CI and the leg stayed green -- the "check that
    quietly does not run" defect, inside the tests written to prove the browser
    lens works. The PR description claimed these failed rather than skipped on
    CI, which was an argued guarantee with nothing behind it.
    """
    return bool(sys.platform.startswith("linux") and os.environ.get("CI"))


_UNAVAILABLE = availability()

if _UNAVAILABLE and _browser_is_expected():  # pragma: no cover - CI guard
    raise RuntimeError(
        f"a browser is required on the Linux CI legs and is not available: "
        f"{_UNAVAILABLE}. The workflow runs `playwright install chromium`; if "
        "that step was removed these tests would silently skip and the leg "
        "would still be green."
    )

_needs_browser = pytest.mark.skipif(
    _UNAVAILABLE is not None, reason=_UNAVAILABLE or "browser available"
)


def test_the_browser_is_present_where_it_is_expected():
    """Loud rather than skipped. A guard that only runs as a module-level raise
    is invisible in a test report; this puts it in the results."""
    if _browser_is_expected():
        assert _UNAVAILABLE is None, _UNAVAILABLE


# --- the origin pin ------------------------------------------------------------------


def test_an_origin_is_a_triple_not_a_string():
    assert Origin.parse("http://localhost:3000/x") == Origin("http", "localhost", 3000)
    assert Origin.parse("https://example.test/") == Origin("https", "example.test", 443)
    assert Origin.parse("http://example.test/") == Origin("http", "example.test", 80)


def test_the_host_is_compared_case_insensitively():
    """DNS is case-insensitive and a hostname that differs only in case is the
    same origin. Refusing it would be a false refusal a user cannot debug."""
    assert Origin.parse("http://LocalHost:3000").admits("http://localhost:3000/a")


@pytest.mark.parametrize(
    "hostile",
    [
        # Starts with the right characters. Defeats `startswith`.
        "http://localhost:3000.evil.test/",
        # Contains the right characters. Defeats a substring test.
        "https://evil.test/?next=http://localhost:3000",
        # Right host, wrong port -- a different origin by every definition.
        "http://localhost:3001/",
        # Right host and port, wrong scheme.
        "https://localhost:3000/",
        # Userinfo trick: the host is evil.test, not localhost.
        "http://localhost:3000@evil.test/",
    ],
)
def test_a_url_that_merely_looks_like_the_origin_is_refused(hostile):
    """Prefix and substring matching on a URL is the same class of defect as
    prefix matching on a path, which the write barrier already refuses."""
    origin = Origin.parse("http://localhost:3000")
    assert origin.admits(hostile) is False, hostile


def test_a_non_http_url_names_no_origin():
    for bad in ("file:///etc/passwd", "javascript:alert(1)", "data:text/html,x"):
        with pytest.raises(BrowserError):
            Origin.parse(bad)


def test_a_non_http_scheme_WITH_a_host_is_refused_on_the_scheme():
    """The case the three above cannot reach.

    `file://`, `javascript:` and `data:` all have no host, so they are refused
    by the host check and the SCHEME check is never exercised -- deleting it
    survived a mutation battery. A URL with a real host and a wrong scheme is
    the only thing that tests it.
    """
    for bad in ("ftp://localhost:3000/", "ws://localhost:3000/", "gopher://x/"):
        with pytest.raises(BrowserError, match="http"):
            Origin.parse(bad)


def test_a_url_with_no_host_is_refused():
    with pytest.raises(BrowserError, match="no host"):
        Origin.parse("http:///nowhere")


def test_browser_error_is_a_whetstone_error():
    """The CLI catches WhetstoneError; anything else reaches a user as a bare
    traceback."""
    assert issubclass(BrowserError, WhetstoneError)


# --- geometry is arithmetic ------------------------------------------------------------


def test_two_separated_boxes_do_not_overlap():
    a = Box(0, 0, 10, 10)
    b = Box(20, 20, 10, 10)
    assert a.intersection_area(b) == 0.0


def test_two_overlapping_boxes_report_their_area():
    a = Box(0, 0, 10, 10)
    b = Box(5, 5, 10, 10)
    assert a.intersection_area(b) == 25.0
    assert b.intersection_area(a) == 25.0, "the measure must be symmetric"


def test_boxes_that_merely_touch_do_not_overlap():
    """Adjacent controls are the normal case, not a defect. An off-by-one here
    reports every button next to another button."""
    a = Box(0, 0, 10, 10)
    b = Box(10, 0, 10, 10)
    assert a.intersection_area(b) == 0.0


def test_a_sub_pixel_overlap_is_still_an_overlap():
    """Floats rather than ints, because rounding makes two boxes that genuinely
    abut look like they overlap by half a pixel -- and the other way round."""
    a = Box(0, 0, 10, 10)
    b = Box(9.5, 0, 10, 10)
    assert a.intersection_area(b) == pytest.approx(5.0)


def test_two_diagonally_separated_boxes_do_not_report_a_positive_overlap():
    """The case the `> 0` guard actually exists for.

    Separated on BOTH axes gives dx < 0 and dy < 0, and their product is
    POSITIVE -- so without the guard the two most obviously unrelated controls
    on a page report the largest overlap of any pair. The touching case does not
    test this: `dx == 0` yields zero either way, which is why relaxing the
    comparison to `>=` survived a battery.
    """
    a = Box(0, 0, 10, 10)
    b = Box(50, 50, 10, 10)
    assert a.intersection_area(b) == 0.0


def test_containment_is_an_overlap_of_the_inner_area():
    outer = Box(0, 0, 100, 100)
    inner = Box(10, 10, 5, 5)
    assert outer.intersection_area(inner) == 25.0


# --- availability says which of two things is missing -----------------------------------


def test_availability_distinguishes_the_package_from_the_binary(monkeypatch):
    """Two failures, two fixes. Telling somebody to install the package when the
    package is there and the browser is not sends them to the wrong place -- and
    the binary is the one people miss, because the import succeeds and the
    failure arrives much later."""
    import builtins

    real_import = builtins.__import__

    def _no_playwright(name, *args, **kwargs):
        if name.startswith("playwright"):
            raise ImportError("no playwright")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _no_playwright)
    reason = availability()
    assert reason is not None
    assert "[browser]" in reason
    assert "playwright install" not in reason, (
        "a missing package must not be reported as a missing browser binary"
    )


# --- driving a real page ----------------------------------------------------------------


class _Server:
    """A local HTTP server, because `file://` has no origin worth pinning.

    `redirect_to` serves a real 302 from `/away`, which is what makes the
    redirect test deterministic -- a `<meta refresh>` races the load state.
    """

    redirect_to: str | None = None

    def __init__(self, root: Path, redirect_to: str | None = None) -> None:
        self.redirect_to = redirect_to
        outer = self

        def _do_get(handler_self):
            if outer.redirect_to and handler_self.path.rstrip("/").endswith("away"):
                handler_self.send_response(302)
                handler_self.send_header("Location", outer.redirect_to)
                handler_self.end_headers()
                return
            http.server.SimpleHTTPRequestHandler.do_GET(handler_self)

        handler = type(
            "H",
            (http.server.SimpleHTTPRequestHandler,),
            {
                "directory": str(root),
                "log_message": lambda *a, **k: None,
                "do_GET": _do_get,
                "__init__": lambda self, *a, **k: http.server.SimpleHTTPRequestHandler.__init__(
                    self, *a, directory=str(root), **k
                ),
            },
        )
        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self) -> str:
        self._thread.start()
        return f"http://127.0.0.1:{self.port}"

    def __exit__(self, *exc: object) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


_OVERLAP = """<!doctype html><html><body style="margin:0">
<button id="a" style="position:absolute;left:0;top:0;width:100px;height:40px">A</button>
<button id="b" style="position:absolute;left:60px;top:0;width:100px;height:40px">B</button>
</body></html>"""

_CLEAN = """<!doctype html><html><body style="margin:0">
<button id="a" style="position:absolute;left:0;top:0;width:100px;height:40px">A</button>
<button id="b" style="position:absolute;left:200px;top:0;width:100px;height:40px">B</button>
</body></html>"""


@_needs_browser
def test_a_real_overlap_is_measured_from_the_dom(tmp_path):
    """The claim this lens exists to make, measured rather than described."""
    (tmp_path / "index.html").write_text(_OVERLAP, encoding="utf-8")
    with _Server(tmp_path) as base, rendered(f"{base}/index.html") as page:
        a = page.box("#a")
        b = page.box("#b")
        assert a is not None and b is not None
        assert a.intersection_area(b) > 0, "two overlapping buttons measured apart"


@_needs_browser
def test_a_clean_page_measures_no_overlap(tmp_path):
    """The counterweight. A measure that always finds an overlap finds nothing."""
    (tmp_path / "index.html").write_text(_CLEAN, encoding="utf-8")
    with _Server(tmp_path) as base, rendered(f"{base}/index.html") as page:
        assert page.box("#a").intersection_area(page.box("#b")) == 0.0


@_needs_browser
def test_an_absent_element_is_none_not_a_zero_box(tmp_path):
    """An element that is not there and one collapsed to nothing are different
    findings, and a zero box makes the first look like the second."""
    (tmp_path / "index.html").write_text(_CLEAN, encoding="utf-8")
    with _Server(tmp_path) as base, rendered(f"{base}/index.html") as page:
        assert page.box("#nonexistent") is None


@_needs_browser
def test_navigating_off_the_origin_is_refused(tmp_path):
    (tmp_path / "index.html").write_text(_CLEAN, encoding="utf-8")
    # NOT combined into one `with`, though SIM117 asks for it. Folding
    # `pytest.raises` into the same statement changes the teardown order and the
    # browser is closed while the page is still being unwound -- measured, it
    # raises TargetClosedError instead of the BrowserError under test.
    with _Server(tmp_path) as base, rendered(f"{base}/index.html") as page:  # noqa: SIM117
        with pytest.raises(BrowserError, match="pinned"):
            page.goto("http://example.test/")


@_needs_browser
def test_a_redirect_off_the_origin_is_caught_after_the_fact(tmp_path):
    """The check-then-navigate gap, forced deterministically.

    A real HTTP 302, not a `<meta http-equiv="refresh">`. The meta version races
    the load state, so the first draft wrapped this in try/except and asserted
    nothing when it did not fire -- a test that passes whether or not the code
    works, which is exactly what deleting the post-navigation check proved by
    surviving a mutation battery.
    """
    (tmp_path / "index.html").write_text(_CLEAN, encoding="utf-8")
    server = _Server(tmp_path)
    # `localhost`, not `example.test`. The target has to RESOLVE: redirecting
    # somewhere that does not, Playwright fails with ERR_NAME_NOT_RESOLVED
    # before the origin check runs, and the test then proves the browser
    # refuses bad DNS rather than that the adapter refuses a foreign origin.
    # Same port, different host string -- a different origin by the triple,
    # served by the very same process.
    server.redirect_to = f"http://localhost:{server.port}/index.html"
    with server as base, rendered(f"{base}/index.html") as page:  # noqa: SIM117
        # Matches the phrase naming WHICH guard fired, not a generic word. The
        # post-navigation check and the pre-read checks share one message now,
        # so "outside" alone would pass if `goto` stopped checking and `box`
        # caught it later instead.
        with pytest.raises(BrowserError, match="refusing to keep this page"):
            page.goto(f"{base}/away")


@_needs_browser
def test_a_screenshot_lands_where_it_was_asked_to(tmp_path):
    (tmp_path / "index.html").write_text(_CLEAN, encoding="utf-8")
    shot = tmp_path / "shot.png"
    with _Server(tmp_path) as base, rendered(f"{base}/index.html") as page:
        page.screenshot(shot)
    assert shot.exists() and shot.stat().st_size > 0


@_needs_browser
def test_the_viewport_is_the_one_that_was_declared(tmp_path):
    """A geometry finding is only true AT a viewport. Measuring at the wrong one
    reports a defect nobody can see."""
    (tmp_path / "index.html").write_text(
        '<!doctype html><body style="margin:0">'
        '<div id="full" style="width:100%;height:10px"></div></body>',
        encoding="utf-8",
    )
    with _Server(tmp_path) as base, rendered(
        f"{base}/index.html", viewport=(640, 480)
    ) as page:
        assert page.box("#full").width == pytest.approx(640, abs=1)


@_needs_browser
def test_rendering_a_page_starts_the_driver_exactly_once(tmp_path, monkeypatch):
    """The assertion behind the claim, in the same commit as the claim.

    `rendered()` used to call `availability()`, which opens a full driver session
    of its own, and then open a second one for the page. A route crawl pays that
    per URL. The docstring now says it opens one; this is what stops that from
    being another argued guarantee with nothing behind it -- the recurring defect
    on this project for three milestones running.
    """
    import playwright.sync_api as sync_api

    real = sync_api.sync_playwright
    starts = []

    def _counted():
        starts.append(1)
        return real()

    monkeypatch.setattr(sync_api, "sync_playwright", _counted)
    (tmp_path / "index.html").write_text(_CLEAN, encoding="utf-8")
    with _Server(tmp_path) as base, rendered(f"{base}/index.html") as page:
        assert page.box("#a") is not None
    assert len(starts) == 1, (
        f"one page render started {len(starts)} driver sessions, not 1"
    )


@_needs_browser
def test_a_page_that_navigates_itself_is_refused_before_it_is_measured(tmp_path):
    """The time-of-check gap `goto` alone leaves open.

    `goto` checks the origin once, after settling. A page can navigate itself
    afterwards -- a delayed redirect, a script, a form -- and `box` or
    `screenshot` would then measure a foreign document and report it as evidence
    about the app under test.
    """
    (tmp_path / "index.html").write_text(_CLEAN, encoding="utf-8")
    server = _Server(tmp_path)
    with server as base, rendered(f"{base}/index.html") as page:
        # Move the page off-origin behind the adapter's back, exactly as a
        # delayed client-side redirect would.
        page._page.goto(f"http://localhost:{server.port}/index.html")
        with pytest.raises(BrowserError, match="refusing to measure an element"):
            page.box("#a")
        with pytest.raises(BrowserError, match="refusing to capture a screenshot"):
            page.screenshot(tmp_path / "shot.png")
    assert not (tmp_path / "shot.png").exists(), (
        "a refused capture must leave no file. A zero-byte screenshot on disk is "
        "evidence about a site that was never under test."
    )


def test_the_page_surface_is_deliberately_narrow():
    """No `evaluate`. Running arbitrary JavaScript in the page would make a
    navigation script an arbitrary program, which is the distinction
    `reproduce.py` learned the hard way about `kind: "pytest"`."""
    import dataclasses

    from whetstone.lenses.rendered_ui.browser import Page

    # Methods AND fields. `origin` is a dataclass field with no class-level
    # default, so it is absent from `dir(Page)` -- checking only `dir()` would
    # miss a capability added the same way, which is the same trap
    # `test_spine_is_lens_agnostic` hit with `LensPack.max_autonomy`.
    methods = {n for n in dir(Page) if not n.startswith("_")}
    fields = {f.name for f in dataclasses.fields(Page) if not f.name.startswith("_")}
    assert methods | fields == {"goto", "box", "screenshot", "origin"}, methods | fields
