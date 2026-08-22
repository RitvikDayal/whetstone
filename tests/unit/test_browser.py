"""The browser adapter.

WHAT IS WORTH ATTACKING: the origin pin. A lens that can navigate anywhere is a
lens that can be steered anywhere, and the page under test is the thing doing
the steering -- it can redirect. So the tests below concentrate on URLs that
LOOK like the declared origin, and on the redirect that happens after the check.

The geometry tests need no browser at all: an intersection of two rectangles is
arithmetic, and testing it against a real page would test the browser instead of
the maths. The page-driving tests use a real Chromium against a `file://`-free
local server, and skip with a reason where the binary is absent -- failing
rather than skipping wherever CI is set, for the reason `sandbox_image` gives.
That guarantee and the server both live in `tests/_browser.py`, so the module
that states the claim is the module that enforces it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# `tests/` is on the path for both test directories; see `tests/conftest.py`.
from _browser import UNAVAILABLE, Server, browser_is_expected, needs_browser
from whetstone.errors import WhetstoneError
from whetstone.lenses.rendered_ui.browser import (
    Box,
    BrowserError,
    Origin,
    Page,
    availability,
    rendered,
)


def test_the_browser_is_present_where_it_is_expected():
    """Loud rather than skipped. A guard that only runs as a module-level raise
    is invisible in a test report; this puts it in the results."""
    if browser_is_expected():
        assert UNAVAILABLE is None, UNAVAILABLE


def test_the_guard_fires_when_ci_has_no_browser(monkeypatch):
    """THE GUARD ITSELF, exercised. Everything above depends on an import-time
    raise that never runs in a healthy environment, so nothing proved it still
    works -- a check that quietly does not run, guarding against checks that
    quietly do not run. The module is re-executed here with CI set and the
    browser reported missing, which is the situation it exists for.
    """
    import importlib

    import _browser as guard

    monkeypatch.setenv("CI", "1")
    monkeypatch.setattr(
        "whetstone.lenses.rendered_ui.browser.availability",
        lambda: "no browser binary (simulated)",
    )
    with pytest.raises(RuntimeError, match="required on every CI leg"):
        importlib.reload(guard)

    # Reloaded clean, or every later importer gets the half-executed module.
    monkeypatch.undo()
    importlib.reload(guard)
    assert guard.UNAVAILABLE == UNAVAILABLE


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


def test_a_page_that_cannot_be_read_at_all_becomes_a_browser_error():
    """`_require_origin` reads `page.url`, and a closed or crashed page raises
    from that attribute. `capture()` catches only BrowserError, so the driver's
    exception escaped as a traceback while the identical failure one line later
    -- a page that moved off-origin -- became a skip reason the user reads.

    No browser needed: the failure is the driver raising, and a stub raises the
    same way a dead page does.
    """

    class _Dead:
        @property
        def url(self) -> str:
            raise RuntimeError("Target page, context or browser has been closed")

    page = Page(_Dead(), Origin.parse("http://127.0.0.1:3000"))

    for call, what in (
        (lambda: page.box("#a"), "measure an element"),
        (lambda: page.screenshot(Path("shot.png")), "capture a screenshot"),
    ):
        with pytest.raises(BrowserError) as caught:
            call()
        assert what in str(caught.value)
        assert "could not be read at all" in str(caught.value)
        assert "RuntimeError" in str(caught.value), (
            "the reason the page could not be read is what makes this skip usable"
        )


def test_a_navigation_failure_becomes_a_browser_error():
    """A timeout, a refused connection, a crashed tab. `capture()` catches only
    BrowserError, so each of these ended a check in a traceback while a page
    that merely moved off-origin produced a readable skip."""

    class _Refusing:
        url = "http://127.0.0.1:3000/x"

        def goto(self, url, timeout=None):
            raise RuntimeError("net::ERR_CONNECTION_REFUSED")

    page = Page(_Refusing(), Origin.parse("http://127.0.0.1:3000"))
    with pytest.raises(BrowserError) as caught:
        page.goto("http://127.0.0.1:3000/x")

    assert "could not load" in str(caught.value)
    assert "ERR_CONNECTION_REFUSED" in str(caught.value), (
        "the driver's reason is what makes this skip actionable"
    )
    assert "127.0.0.1:3000/x" in str(caught.value), (
        "'it did not load' is not actionable without saying what did not load"
    )


def test_a_browser_that_cannot_start_becomes_a_browser_error(monkeypatch):
    """THE BINARY BEING PRESENT IS NOT THE BINARY STARTING. A runner with an
    unusable sandbox, a missing shared library, or no writable profile
    directory passes `_missing_binary` and fails at launch -- and the lens died
    with a traceback rather than saying the environment cannot run a browser.
    """
    import playwright.sync_api as sync_api

    from whetstone.lenses.rendered_ui import browser as browser_module

    class _Chromium:
        def launch(self, **_kw):
            raise RuntimeError("Failed to launch: no usable sandbox")

    class _Play:
        chromium = _Chromium()

        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(sync_api, "sync_playwright", lambda: _Play())
    monkeypatch.setattr(browser_module, "_missing_binary", lambda _play: None)

    with pytest.raises(BrowserError) as caught:  # noqa: SIM117
        with rendered("http://127.0.0.1:3000/x"):
            pass

    assert "could not be started" in str(caught.value)
    assert "no usable sandbox" in str(caught.value)


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


_OVERLAP = """<!doctype html><html><body style="margin:0">
<button id="a" style="position:absolute;left:0;top:0;width:100px;height:40px">A</button>
<button id="b" style="position:absolute;left:60px;top:0;width:100px;height:40px">B</button>
</body></html>"""

_CLEAN = """<!doctype html><html><body style="margin:0">
<button id="a" style="position:absolute;left:0;top:0;width:100px;height:40px">A</button>
<button id="b" style="position:absolute;left:200px;top:0;width:100px;height:40px">B</button>
</body></html>"""


@needs_browser
def test_a_real_overlap_is_measured_from_the_dom(tmp_path):
    """The claim this lens exists to make, measured rather than described."""
    (tmp_path / "index.html").write_text(_OVERLAP, encoding="utf-8")
    with Server(tmp_path) as base, rendered(f"{base}/index.html") as page:
        a = page.box("#a")
        b = page.box("#b")
        assert a is not None and b is not None
        assert a.intersection_area(b) > 0, "two overlapping buttons measured apart"


@needs_browser
def test_a_clean_page_measures_no_overlap(tmp_path):
    """The counterweight. A measure that always finds an overlap finds nothing."""
    (tmp_path / "index.html").write_text(_CLEAN, encoding="utf-8")
    with Server(tmp_path) as base, rendered(f"{base}/index.html") as page:
        assert page.box("#a").intersection_area(page.box("#b")) == 0.0


@needs_browser
def test_an_absent_element_is_none_not_a_zero_box(tmp_path):
    """An element that is not there and one collapsed to nothing are different
    findings, and a zero box makes the first look like the second."""
    (tmp_path / "index.html").write_text(_CLEAN, encoding="utf-8")
    with Server(tmp_path) as base, rendered(f"{base}/index.html") as page:
        assert page.box("#nonexistent") is None


@needs_browser
def test_navigating_off_the_origin_is_refused(tmp_path):
    (tmp_path / "index.html").write_text(_CLEAN, encoding="utf-8")
    # NOT combined into one `with`, though SIM117 asks for it. Folding
    # `pytest.raises` into the same statement changes the teardown order and the
    # browser is closed while the page is still being unwound -- measured, it
    # raises TargetClosedError instead of the BrowserError under test.
    with Server(tmp_path) as base, rendered(f"{base}/index.html") as page:  # noqa: SIM117
        with pytest.raises(BrowserError, match="pinned"):
            page.goto("http://example.test/")


@needs_browser
def test_a_redirect_off_the_origin_is_caught_after_the_fact(tmp_path):
    """The check-then-navigate gap, forced deterministically.

    A real HTTP 302, not a `<meta http-equiv="refresh">`. The meta version races
    the load state, so the first draft wrapped this in try/except and asserted
    nothing when it did not fire -- a test that passes whether or not the code
    works, which is exactly what deleting the post-navigation check proved by
    surviving a mutation battery.
    """
    (tmp_path / "index.html").write_text(_CLEAN, encoding="utf-8")
    server = Server(tmp_path)
    # A SECOND SERVER, not `localhost` on the same port. The target has to
    # RESOLVE -- redirect somewhere that does not and Playwright fails with
    # ERR_NAME_NOT_RESOLVED before the origin check runs, and the test then
    # proves the browser refuses bad DNS rather than that the adapter refuses a
    # foreign origin. `localhost` was that reachable target until the Windows
    # legs started running these: `Server` binds IPv4 only, and where
    # `localhost` resolves to `::1` first the connection is refused before
    # anything under test executes. A second listener on a different port is a
    # different origin by the triple, needs no name resolution at all, and
    # cannot depend on which family the host prefers.
    other = Server(tmp_path)
    with other as other_base:
        server.redirect_to = f"{other_base}/index.html"
        _assert_refuses_the_redirect(server, tmp_path)


def _assert_refuses_the_redirect(server, tmp_path) -> None:
    with server as base, rendered(f"{base}/index.html") as page:  # noqa: SIM117
        # Matches the phrase naming WHICH guard fired, not a generic word. The
        # post-navigation check and the pre-read checks share one message now,
        # so "outside" alone would pass if `goto` stopped checking and `box`
        # caught it later instead.
        with pytest.raises(BrowserError, match="refusing to keep this page"):
            page.goto(f"{base}/away")


@needs_browser
def test_a_screenshot_lands_where_it_was_asked_to(tmp_path):
    (tmp_path / "index.html").write_text(_CLEAN, encoding="utf-8")
    shot = tmp_path / "shot.png"
    with Server(tmp_path) as base, rendered(f"{base}/index.html") as page:
        page.screenshot(shot)
    assert shot.exists() and shot.stat().st_size > 0


@needs_browser
def test_the_viewport_is_the_one_that_was_declared(tmp_path):
    """A geometry finding is only true AT a viewport. Measuring at the wrong one
    reports a defect nobody can see."""
    (tmp_path / "index.html").write_text(
        '<!doctype html><body style="margin:0">'
        '<div id="full" style="width:100%;height:10px"></div></body>',
        encoding="utf-8",
    )
    with Server(tmp_path) as base, rendered(
        f"{base}/index.html", viewport=(640, 480)
    ) as page:
        assert page.box("#full").width == pytest.approx(640, abs=1)


@needs_browser
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
    with Server(tmp_path) as base, rendered(f"{base}/index.html") as page:
        assert page.box("#a") is not None
    assert len(starts) == 1, (
        f"one page render started {len(starts)} driver sessions, not 1"
    )


@needs_browser
def test_a_page_that_navigates_itself_is_refused_before_it_is_measured(tmp_path):
    """The time-of-check gap `goto` alone leaves open.

    `goto` checks the origin once, after settling. A page can navigate itself
    afterwards -- a delayed redirect, a script, a form -- and `box` or
    `screenshot` would then measure a foreign document and report it as evidence
    about the app under test.
    """
    (tmp_path / "index.html").write_text(_CLEAN, encoding="utf-8")
    server = Server(tmp_path)
    other = Server(tmp_path)
    with other as other_base, server as base, rendered(f"{base}/index.html") as page:
        # Move the page off-origin behind the adapter's back, exactly as a
        # delayed client-side redirect would. A second listener rather than
        # `localhost` on this port, for the reason the redirect test gives.
        page._page.goto(f"{other_base}/index.html")
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
