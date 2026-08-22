"""The browser adapter, and the origin it is pinned to.

INSIDE THE LENS, NOT THE SPINE. This module lives under `lenses/rendered_ui/`
because `tests/unit/test_spine_is_lens_agnostic.py` says the spine may not learn
about browsers -- and that gate was written before this file existed, so it is a
measurement rather than a description. Everything Playwright-shaped is here.

PINNED BY SCHEME, HOST AND PORT. Not by prefix. `https://evil.test/?x=http://localhost:3000`
starts with nothing useful and `http://localhost:3000.evil.test` starts with the
right characters -- prefix matching on a URL is the same class of defect as
prefix matching on a path, which this codebase already refuses in the write
barrier. The comparison is on the parsed triple.

A REDIRECT IS A NAVIGATION. The origin is re-checked after the page settles, not
only before the request: a page under test can redirect anywhere, and a check
that only looks at what was asked for is a check-then-navigate gap with the same
shape as check-then-write.

NO PLAYWRIGHT, NO LENS -- AND IT SAYS SO. An absent package or an absent browser
binary produces a reason that reaches the user, exactly as a missing `pip-audit`
does for `deps` and a missing Docker does for `reproduce`. It never silently
finds nothing, because a lens that reports nothing and a lens that could not run
are the same output and opposite facts.
"""

from __future__ import annotations

import contextlib
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from ...errors import WhetstoneError

# A page that never settles is a run that never finishes.
_NAVIGATION_TIMEOUT_MS = 30_000
# How long to wait for the network to go quiet before measuring. Measured
# rather than guessed is the goal; this is the ceiling on that wait.
_SETTLE_TIMEOUT_MS = 10_000


class BrowserError(WhetstoneError):
    """A browser that could not be driven, and why."""


@dataclass(frozen=True)
class Origin:
    """The one origin this lens may visit, as a comparable triple.

    A string comparison is what this type exists to prevent. `startswith` on a
    URL is defeated by `http://localhost:3000.evil.test`, and a substring test by
    `https://evil.test/?next=http://localhost:3000` -- both of which read as the
    declared origin to a human skimming the code.
    """

    scheme: str
    host: str
    port: int

    @classmethod
    def parse(cls, url: str) -> Origin:
        # `urlparse` is lazy: it accepts anything and raises from `.port` when
        # the authority is malformed. `http://localhost:3000.evil.test/` is the
        # live example -- it parses, and asking for its port raises ValueError
        # with "3000.evil.test". Uncaught, that escapes `admits()` as a crash
        # rather than a refusal, so a hostile URL takes the run down instead of
        # being turned away. Measured, not theorised: it was the first
        # parametrised case to fail.
        try:
            parsed = urlparse(url)
            port = parsed.port
        except ValueError as exc:
            raise BrowserError(
                f"{url!r} is not a well-formed URL ({exc}), so it names no "
                "origin and the browser will not visit it."
            ) from exc
        if parsed.scheme not in ("http", "https"):
            raise BrowserError(
                f"{url!r} is not an http(s) URL, so there is no origin to pin "
                "the browser to."
            )
        if not parsed.hostname:
            raise BrowserError(f"{url!r} has no host, so it names no origin.")
        # No `.lower()`: `urlparse().hostname` already lowercases, so the call
        # was dead code that read as a safeguard. Removing it survived a
        # mutation battery, which is how it was found -- and a redundant
        # safeguard is worse than none, because it invites trust.
        return cls(
            parsed.scheme,
            parsed.hostname,
            port or (443 if parsed.scheme == "https" else 80),
        )

    def admits(self, url: str) -> bool:
        try:
            return Origin.parse(url) == self
        except BrowserError:
            return False

    def __str__(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"


@dataclass(frozen=True)
class Box:
    """A DOM bounding box, in CSS pixels, as the browser measured it.

    Floats because that is what the browser returns; rounding here would make
    two boxes that genuinely abut look like an overlap of half a pixel.
    """

    x: float
    y: float
    width: float
    height: float

    @property
    def right(self) -> float:
        return self.x + self.width

    @property
    def bottom(self) -> float:
        return self.y + self.height

    def intersection_area(self, other: Box) -> float:
        """Overlapping area in square CSS pixels, computed here rather than
        claimed by a model.

        This is invariant 2 in the shape this lens takes: "these two controls
        overlap" is a candidate, and a non-empty intersection of two measured
        rectangles is evidence.
        """
        dx = min(self.right, other.right) - max(self.x, other.x)
        dy = min(self.bottom, other.bottom) - max(self.y, other.y)
        # BOTH must be positive, and the reason is not the touching case --
        # `dx == 0` gives an area of zero either way. It is the DIAGONAL case:
        # two boxes separated on both axes give dx < 0 AND dy < 0, whose product
        # is POSITIVE. Without this guard, the two most obviously unrelated
        # controls on a page report the largest overlap. Relaxing the comparison
        # to `>=` survived a battery; a test for the diagonal case is what
        # catches it.
        return dx * dy if dx > 0 and dy > 0 else 0.0


def _missing_package() -> str | None:
    """The import half of the check. No driver session behind it, so it is free."""
    try:
        import playwright.sync_api  # noqa: F401
    except ImportError:
        return (
            "playwright is not installed, so no page can be rendered. Install "
            "the browser extra: `pip install 'whetstone-cli[browser]'`. Nothing "
            "was checked."
        )
    return None


def _missing_binary(play: object) -> str | None:
    """The binary half, checked inside a session the CALLER already opened.

    Split from `availability()` so `rendered()` does not pay for a second driver
    start. Same split, and the same reason, as `sandbox.py`: the probe and the
    run are separate so the caller can decide which one it is doing.
    """
    try:
        path = Path(play.chromium.executable_path)
    except Exception as exc:  # noqa: BLE001 -- playwright raises its own types
        return f"playwright could not start: {exc}"
    if not path.exists():
        return (
            "playwright is installed but its browser binary is not. Run "
            "`playwright install chromium`. Nothing was rendered."
        )
    return None


def availability() -> str | None:
    """None when a browser can be driven, or the reason it cannot.

    Two separate failures with two separate fixes, and conflating them sends
    somebody to the wrong place: the PACKAGE may be absent (`pip install
    'whetstone-cli[browser]'`) or the BROWSER BINARY may be
    (`playwright install chromium`). The second is the one people miss, because
    the import succeeds and the failure arrives much later.

    THIS OPENS A DRIVER SESSION, so `rendered()` deliberately does not call it --
    it runs the two halves itself, the binary half inside the session it was
    going to open anyway. A route crawl renders many URLs and would otherwise pay
    two driver starts per page instead of one. Not cached to achieve that: a
    cached probe reads whatever the first caller happened to see, which made this
    function untestable the moment a test primed it at import time.
    """
    missing = _missing_package()
    if missing is not None:
        return missing
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as play:
            return _missing_binary(play)
    except Exception as exc:  # noqa: BLE001 -- playwright raises its own types
        return f"playwright could not start: {exc}"


@dataclass
class Page:
    """What a stage may do to a page. Deliberately four things.

    Narrow because the lens does not need more, and because every method here
    is a capability a model's navigation script can reach. There is no
    `evaluate`: running arbitrary JavaScript in the page would make the
    navigation script an arbitrary program, which is the distinction
    `reproduce.py` learned the hard way about `kind: "pytest"`.
    """

    _page: object
    origin: Origin

    def goto(self, url: str) -> None:
        if not self.origin.admits(url):
            raise BrowserError(
                f"refusing to navigate to {url!r}: this lens is pinned to "
                f"{self.origin}. Production browsing is opt-in per invocation "
                "and read-only."
            )
        try:
            self._page.goto(url, timeout=_NAVIGATION_TIMEOUT_MS)
        except Exception as exc:  # noqa: BLE001 - driver errors share no base
            # A TIMEOUT, A REFUSED CONNECTION, A CRASHED TAB. `capture()`
            # catches only BrowserError, so every one of these ended the check
            # in a traceback while a page that merely moved off-origin got a
            # readable skip. The URL travels in the message because "it did not
            # load" is not actionable without saying what did not load.
            raise BrowserError(
                f"could not load {url!r}: {type(exc).__name__}: {exc}. "
                f"Nothing was measured or captured."
            ) from exc
        self._settle()
        # AFTER settling, not only before the request. A page can redirect
        # anywhere, and checking only what was asked for is a
        # check-then-navigate gap.
        self._require_origin("keep this page")

    def _settle(self) -> None:
        """Wait for the network to go quiet, then for the document to be ready.

        NOT a fixed sleep. A sleep long enough to be reliable is long enough to
        make a route crawl unusable, and one short enough to be quick is a coin
        flip -- which the design names directly as the browser lens's flakiness
        risk. A timeout here is not fatal: a page that never goes idle can still
        be measured, and the second pass is what decides whether the measurement
        was stable.
        """
        for state in ("networkidle", "load"):
            # Suppressed deliberately, and the docstring above says why: a page
            # that never goes idle can still be measured, and the second pass is
            # what decides whether the measurement was stable.
            with contextlib.suppress(Exception):
                self._page.wait_for_load_state(state, timeout=_SETTLE_TIMEOUT_MS)

    def _require_origin(self, what: str) -> None:
        """Refuse to read from a page that has moved off the origin.

        `goto()` checks once, after settling. That is a time-of-check gap: a
        page can navigate itself afterwards -- a delayed redirect, a script, a
        form -- and `box()` or `screenshot()` would then measure a foreign
        document and report it as evidence about the app under test. Checked
        before every read rather than only at navigation.
        """
        try:
            landed = self._page.url
        except Exception as exc:  # noqa: BLE001 - driver errors have no shared base
            # A CLOSED OR CRASHED PAGE RAISES HERE, and `capture()` catches
            # only BrowserError -- so a driver error escaped as a traceback
            # while the same failure one line later became a readable skip.
            # Converted rather than suppressed: refusing to read is exactly
            # what this method is for, and "the page is gone" is a reason.
            raise BrowserError(
                f"refusing to {what}: the page could not be read at all "
                f"({type(exc).__name__}: {exc}). Nothing was measured or "
                f"captured."
            ) from exc
        if not self.origin.admits(landed):
            # Deliberately says WHERE it is and not WHEN it moved. The same
            # guard serves a redirect during load and a script navigating the
            # page an hour later, and a message asserting "it navigated after it
            # was loaded" is false for the first of those.
            raise BrowserError(
                f"refusing to {what}: the page is at {landed!r}, outside "
                f"{self.origin}. Whatever is there is a different site, so "
                "nothing was measured or captured."
            )

    def box(self, selector: str) -> Box | None:
        """The element's bounding box, or None when it is absent or invisible.

        None rather than a zero box: an element that is not there and an element
        collapsed to nothing are different findings, and a zero box would make
        the first look like the second.
        """
        self._require_origin("measure an element")
        try:
            element = self._page.query_selector(selector)
            if element is None:
                return None
            raw = element.bounding_box()
        except Exception as exc:  # noqa: BLE001 - driver errors share no base
            # THE SELECTOR CAME FROM A MODEL. `check.selector_a` is drive-stage
            # payload, so `div[` or `:has(` reaches Playwright's parser and
            # raises from there -- and the page can also close between the
            # origin check above and this call, a window that check cannot
            # shut. `capture()` catches only BrowserError, so both ended the
            # run in a traceback while a page that moved off-origin one line
            # earlier produced a readable skip. The selector travels: which of
            # the two it was is the whole diagnosis.
            raise BrowserError(
                f"could not measure {selector!r}: {type(exc).__name__}: {exc}"
            ) from exc
        if raw is None:
            return None
        return Box(raw["x"], raw["y"], raw["width"], raw["height"])

    def screenshot(self, path: Path) -> None:
        self._require_origin("capture a screenshot")
        try:
            self._page.screenshot(path=str(path))
        except Exception as exc:  # noqa: BLE001 - driver errors share no base
            # A FULL OR READ-ONLY DISK, or a page that died mid-write. The
            # image is the evidence, so failing to write it is exactly the
            # thing a reader must be told rather than left to infer.
            raise BrowserError(
                f"could not capture a screenshot to {path}: "
                f"{type(exc).__name__}: {exc}"
            ) from exc


@contextmanager
def rendered(
    url: str, *, viewport: tuple[int, int] = (1280, 800)
) -> Iterator[Page]:
    """A page at *url*, at *viewport*, pinned to that URL's origin.

    Headless, and the browser is closed whatever happens -- a leaked Chromium
    holds a profile directory and, on a shared runner, several hundred megabytes.
    """
    blocked = _missing_package()
    if blocked is not None:
        raise BrowserError(blocked)

    from playwright.sync_api import sync_playwright

    # Before the session: a malformed URL should not pay for a driver start.
    origin = Origin.parse(url)
    width, height = viewport
    with sync_playwright() as play:
        # The binary half, inside the one session this function opens. Calling
        # `availability()` here instead would open a second one per page.
        blocked = _missing_binary(play)
        if blocked is not None:
            raise BrowserError(blocked)
        try:
            browser = play.chromium.launch(headless=True)
        except Exception as exc:  # noqa: BLE001 - driver errors share no base
            # THE BINARY BEING PRESENT IS NOT THE BINARY STARTING. A runner
            # with a sandbox it cannot use, a missing shared library, or no
            # writable profile directory passes `_missing_binary` and fails
            # here -- and the lens then died with a traceback rather than
            # telling the user their environment cannot run a browser.
            raise BrowserError(
                f"the browser could not be started: {type(exc).__name__}: "
                f"{exc}. Nothing was rendered."
            ) from exc
        context = None
        try:
            context = browser.new_context(
                viewport={"width": width, "height": height}
            )
            page = context.new_page()
            wrapped = Page(page, origin)
            wrapped.goto(url)
            yield wrapped
        finally:
            # CONTEXT FIRST, then the browser. Closing the browser alone leaves
            # the context's connection work in flight, and Playwright's sync
            # wrapper then prints "Task was destroyed but it is pending" and a
            # TargetClosedError on interpreter shutdown -- on a PASSING run.
            # Noise on a green run is how people learn to stop reading output,
            # which is the same failure as a skip that fires every time.
            if context is not None:
                with contextlib.suppress(Exception):
                    context.close()
            # SUPPRESSED FOR THE SAME REASON, and it matters more here. This
            # runs while a BrowserError from the body is unwinding, and an
            # exception raised in `finally` REPLACES the one in flight. The
            # caller catches BrowserError and turns it into a skip a user can
            # read; a TargetClosedError from teardown escapes that handler
            # instead, so a check would end in a traceback rather than a
            # reason. Losing the close is a leaked process, and it is already
            # the failing path -- losing the diagnosis is losing the run.
            with contextlib.suppress(Exception):
                browser.close()
