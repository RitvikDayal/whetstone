"""The control plane, rendered by a real browser, compared against the API.

WHY A BROWSER AND NOT A JSON ASSERTION. Every failure this milestone exists to
prevent happened at the RENDER layer, not in the query: the falsifier's verdict
was computed correctly and never reached the list; a grade D rendered
identically to a grade A; `get_last_run` selected the status column and dropped
it. A test that stops at `/api/findings` is one layer below all three.

So this drives Chromium at the real server, reads the rendered DOM, and asserts
it against what the API returned for the same store. The two can only agree if
the whole chain -- SQL ordering, read model, JSON, CSP, bundle, React -- holds.

IT ALSO PROVES THE PAGE LOADS AT ALL, which is the definition-of-done item that
no unit test can stand in for. A Content-Security-Policy that blocks the bundle,
a token that never reaches the app, an asset path that 404s: each produces a
blank page and a green unit suite.
"""

from __future__ import annotations

import contextlib
import json
import threading
import urllib.request
from pathlib import Path

import pytest

# NOT `pytest.importorskip`. That skips the WHOLE MODULE when the extra
# is absent -- and on a CI leg that dropped `--all-extras`, every test in
# here would skip while the leg stayed green. `_bundle` raises instead
# wherever CI is set, and skips only on a developer machine.
from _bundle import UI_EXTRA_MISSING  # noqa: E402
from whetstone.config.loader import load_config
from whetstone.grade import Grade
from whetstone.lenses.base import Candidate, Evidence, EvidenceKind
from whetstone.server import serve as serve_module
from whetstone.server.security import TOKEN_HEADER
from whetstone.severity import Severity
from whetstone.store.db import connect
from whetstone.store.findings import upsert

pytestmark = pytest.mark.skipif(
    UI_EXTRA_MISSING is not None, reason=UI_EXTRA_MISSING or "ui extra present"
)

from _browser import needs_browser  # noqa: E402

# FAILS rather than skips wherever CI is set -- see `tests/_bundle.py`. A local
# `skipif` would let a forgotten `npm run build` turn every test in this file
# into a skip on a green leg, covering none of the control plane.
from _bundle import needs_bundle  # noqa: E402

_SEEDS = [
    ("app/pay.py", "divide by zero on an empty basket", "critical", "A"),
    ("app/auth.py", "the session token never expires", "high", "A"),
    ("app/ui.py", "two controls overlap at 1280x800", "medium", "B"),
    ("app/dep.py", "CVE-2026-0001 in a transitive package", "high", None),
    ("app/no.py", "the falsifier refuted this one", "critical", "D"),
]


@pytest.fixture
def served(tmp_path: Path):
    """A real project, a real store with findings, and a real server."""
    import uvicorn

    from whetstone.server.app import create_app

    (tmp_path / "whetstone.yaml").write_text(
        "version: 1\nproject:\n  name: rendered\nstate_dir: .state\n",
        encoding="utf-8",
    )
    state = tmp_path / ".state"
    # `closing`, not a bare `connect`: a seeding failure between here and the
    # close left the connection -- and on Windows the file handle -- open for
    # the rest of the session.
    with contextlib.closing(connect(state)) as conn:
        conn.execute(
            "INSERT INTO runs (id, tier, scope_mode, file_count, started_at, "
            "status, skipped_json) VALUES ('run-0000000001','deep','full',5,"
            "'2020-01-01T00:00:00+00:00','complete',?)",
            ('["hygiene/deps: pip-audit is not installed"]',),
        )
        for index, (subject, title, severity, grade) in enumerate(_SEEDS):
            upsert(
                conn,
                Candidate(
                    lens="code-defects",
                    rule_id=f"r{index}",
                    subject=subject,
                    title=title,
                    detail=f"Detail for {title}.",
                    severity=Severity(severity),
                    evidence=Evidence(
                        kind=EvidenceKind.metric, summary="seeded", data={}
                    ),
                    grade=None if grade is None else Grade(grade),
                    grade_reason=None if grade is None else f"graded {grade}",
                ),
                "run-0000000001",
                # PAST, unambiguously. `get_last_run` orders by `started_at`,
                # so a fixture stamped with today's date at 10:00 outranks a
                # run started live before 10:00 UTC -- a fixture claiming to
                # have happened in the future.
                "2020-01-01T00:00:00+00:00",
            )

    token = serve_module.mint_token()
    sock = serve_module.bind()
    port = sock.getsockname()[1]
    app = create_app(
        config=load_config(tmp_path / "whetstone.yaml"),
        project_root=tmp_path,
        state_root=state,
        token=token,
        port=port,
    )
    server = uvicorn.Server(
        uvicorn.Config(app, access_log=False, log_level="error", lifespan="off")
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    _wait_for(base, token)
    try:
        yield base, token
    finally:
        server.should_exit = True
        thread.join(timeout=30)


def _wait_for(base: str, token: str) -> None:
    import time

    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            _api(base, token)
            return
        except Exception:  # noqa: BLE001 - any failure means not up yet
            time.sleep(0.05)
    raise AssertionError("the control plane never started listening")


def _api(base: str, token: str) -> dict:
    request = urllib.request.Request(f"{base}/api/findings")
    request.add_header(TOKEN_HEADER, token)
    with urllib.request.urlopen(request, timeout=30) as response:
        return json.loads(response.read())


@pytest.fixture
def page(served):
    """A Chromium page that has loaded the control plane, with console errors
    collected so a silently broken bundle cannot pass as a working one."""
    from playwright.sync_api import sync_playwright

    base, token = served
    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            context = browser.new_context()
            page = context.new_page()
            problems: list[str] = []
            page.on(
                "console",
                lambda msg: problems.append(msg.text) if msg.type == "error" else None,
            )
            page.on("pageerror", lambda exc: problems.append(str(exc)))
            # `domcontentloaded` plus an explicit wait, NOT `networkidle`.
            # This page holds an event stream open on the Run tab, and
            # `networkidle` waits for 500ms of no network activity -- so it is
            # a heuristic that this app can legitimately never satisfy. It
            # timed out once during end-to-end verification for exactly that
            # reason. Waiting for the element that proves React mounted is the
            # thing actually being asserted.
            page.goto(f"{base}/#t={token}", wait_until="domcontentloaded")
            page.wait_for_selector("h1", timeout=60_000)
            page.wait_for_selector("nav button", timeout=60_000)
            # AND WAIT FOR THE DATA. `h1` and the tab bar render the moment
            # React mounts, which is BEFORE `/api/findings` has answered -- so
            # this fixture used to hand back a page still showing "Reading the
            # queue". On this machine the fetch won that race every time; on
            # the Windows 3.12 CI leg it did not, and two tests failed against
            # a page that was working correctly and simply had not finished.
            #
            # `served` always seeds five findings, so their presence is the
            # signal that the first render is complete. Waiting on a spinner to
            # disappear would pass for a page that never loaded at all.
            page.wait_for_selector("li.finding", timeout=60_000)
            yield page, problems, base, token
        finally:
            browser.close()


@needs_browser
@needs_bundle
def test_the_control_plane_actually_renders(page):
    """The definition-of-done item. A blank page passes every unit test."""
    rendered, problems, _base, _token = page

    assert problems == [], f"the page reported errors: {problems}"
    assert rendered.locator("h1").inner_text().strip() == "Whetstone"
    assert rendered.locator("li.finding").count() == len(_SEEDS)


@needs_browser
@needs_bundle
def test_the_content_security_policy_does_not_block_the_bundle(page):
    """A CSP tight enough to be worth having is tight enough to break the page.

    `script-src 'self'` with no `'unsafe-inline'` only works because the Vite
    build is configured to emit no inline module-preload polyfill. If that
    setting were lost, the bundle would be blocked, React would never mount,
    and `#root` would be empty -- with every Python test still green.
    """
    rendered, problems, _base, _token = page

    assert not any("Content Security Policy" in p for p in problems), problems
    assert rendered.locator("#root").inner_html().strip() != ""


@needs_browser
@needs_bundle
def test_the_rendered_order_matches_the_api_order(page):
    """RENDER PARITY. The API and the DOM, over the same store, in one test."""
    rendered, _problems, base, token = page

    payload = _api(base, token)
    api_titles = [f["title"] for f in payload["findings"]]
    dom_titles = [
        el.inner_text().strip()
        for el in rendered.locator("li.finding .title").all()
    ]

    assert dom_titles == api_titles
    # And the order is the store's, not severity-first: a grade B outranks an
    # ungraded high, and both outrank the killed critical.
    assert dom_titles.index("two controls overlap at 1280x800") < dom_titles.index(
        "CVE-2026-0001 in a transitive package"
    )
    assert dom_titles[-1] == "the falsifier refuted this one"


@needs_browser
@needs_bundle
def test_a_killed_finding_says_killed_on_screen(page):
    """The M1b-1 defect, checked where it actually happened -- on a screen."""
    rendered, _problems, base, token = page

    killed = [f for f in _api(base, token)["findings"] if f["killed"]]
    assert killed, "the fixture must contain a killed finding"

    # Case-INSENSITIVE: the verdict cell is `text-transform: uppercase`, so
    # `inner_text()` returns what the user actually sees ("KILLED") rather than
    # what the source says. Asserting the source spelling would fail against a
    # correct render, which is a test that punishes the thing it is checking.
    text = rendered.locator("ul.findings").inner_text().lower()
    assert "killed" in text
    assert rendered.locator("li.finding.killed").count() == len(killed)


@needs_browser
@needs_bundle
def test_an_ungraded_finding_is_not_shown_as_killed(page):
    """`hygiene` does not grade, and absent is not refuted."""
    rendered, _problems, _base, _token = page

    row = rendered.locator("li.finding", has_text="CVE-2026-0001")
    assert "killed" not in (row.get_attribute("class") or "")
    assert "not graded" in row.inner_text().lower()


@needs_browser
@needs_bundle
def test_the_skip_list_reaches_the_screen(page):
    """A run that examined less than it claimed has to say so, on the surface
    a user reads -- not only in the JSON."""
    rendered, _problems, _base, _token = page

    assert "Not everything was checked" in rendered.locator("body").inner_text()
    assert "pip-audit is not installed" in rendered.locator("body").inner_text()


@needs_browser
@needs_bundle
def test_a_page_opened_without_a_token_explains_itself(served):
    """The second-tab and bookmark case. It must not be a blank page."""
    from playwright.sync_api import sync_playwright

    base, _token = served
    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(base, wait_until="domcontentloaded")
            page.wait_for_selector("div.banner-alarm", timeout=60_000)
            text = page.locator("body").inner_text()
            assert "No session token" in text
            assert "whetstone ui" in text
        finally:
            browser.close()


@needs_browser
@needs_bundle
def test_the_token_survives_a_reload(served):
    """`sessionStorage`, not memory. Held in memory alone, F5 destroyed the
    session and left a blank page -- and reloading is the first thing anyone
    does when a page looks wrong."""
    from playwright.sync_api import sync_playwright

    base, token = served
    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            page = browser.new_page()
            # `wait_for_selector` before every `count()`: `count()` does not
            # auto-wait, so it reads whatever is on the page at that instant --
            # zero, on a runner slower than this one.
            page.goto(f"{base}/#t={token}", wait_until="domcontentloaded")
            page.wait_for_selector("li.finding", timeout=60_000)
            assert page.locator("li.finding").count() == len(_SEEDS)

            page.reload(wait_until="domcontentloaded")

            page.wait_for_selector("li.finding", timeout=60_000)
            assert page.locator("li.finding").count() == len(_SEEDS), (
                "the session did not survive a reload"
            )
            # And the token is no longer in the address bar.
            assert "#t=" not in page.url
        finally:
            browser.close()


@needs_browser
@needs_bundle
def test_an_in_flight_run_is_picked_up_again_after_leaving_the_tab(served):
    """Switching tabs unmounts Run and aborts its stream.

    With the ticket in component state the user could never get back to a run
    that was still going -- it would keep running server-side with no way to
    watch it. The ticket is parked in `sessionStorage` and reattached on mount.

    DETERMINISTIC, not a race. Driving this through a real run would mean
    switching tabs inside the second or so a `hygiene` run takes. Instead the
    event file is written by hand, mid-run -- `run_started` and a `lens_started`
    with no terminal event -- and the ticket is planted in `sessionStorage`.
    That is exactly the state the browser would be in, without the timing.
    """
    from playwright.sync_api import sync_playwright

    from whetstone.server import runs as runs_module

    base, token = served
    state = Path(_state_root_for(base, token))
    ticket = runs_module.new_ticket()
    events = runs_module.events_path(state, ticket)
    events.parent.mkdir(parents=True, exist_ok=True)
    events.write_text(
        json.dumps(
            {
                "kind": "run_started",
                "run_id": "run-inflight01",
                "tier": "deep",
                "file_count": 5,
                "lens_count": 1,
                "lenses": ["code-defects"],
                "skips": [],
            }
        )
        + "\n"
        + json.dumps(
            {"kind": "lens_started", "run_id": "run-inflight01", "lens": "code-defects"}
        )
        + "\n",
        encoding="utf-8",
    )

    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            page = browser.new_page()
            page.goto(f"{base}/#t={token}", wait_until="domcontentloaded")
            page.wait_for_selector("nav button", timeout=60_000)
            page.evaluate(
                "t => sessionStorage.setItem('whetstone.run.ticket', t)", ticket
            )

            page.get_by_role("button", name="run", exact=True).click()

            page.wait_for_selector("ol.events li", timeout=60_000)
            shown = page.locator("ol.events").inner_text()
            assert "code-defects" in shown
            assert "run-inflight01" in page.locator("main").inner_text()
        finally:
            browser.close()
            # The stream is still tailing a file with no terminal event; the
            # 60-second no-progress timeout in `tail` ends it.
            events.write_text(
                events.read_text(encoding="utf-8")
                + json.dumps(
                    {
                        "kind": "run_finished",
                        "run_id": "run-inflight01",
                        "status": "complete",
                        "new": 0,
                        "seen": 0,
                        "skips": [],
                    }
                )
                + "\n",
                encoding="utf-8",
            )


def _state_root_for(base: str, token: str) -> str:
    """The state directory this served fixture is using.

    The PROJECT ROOT is read off the API; the `.state` suffix is this file's
    own knowledge of what the fixture's `whetstone.yaml` declares. An earlier
    docstring said the whole path was read back "rather than reconstructed",
    which was half true and the wrong half -- if the fixture's `state_dir`
    changed, this would still point at `.state` and the test would fail
    somewhere unhelpful.
    """
    request = urllib.request.Request(f"{base}/api/config")
    request.add_header(TOKEN_HEADER, token)
    with urllib.request.urlopen(request, timeout=30) as response:
        project_root = json.loads(response.read())["project_root"]
    return str(Path(project_root) / ".state")


@needs_browser
@needs_bundle
def test_starting_a_run_parks_its_ticket_before_anything_can_lose_it(served):
    """The other half: the ticket has to be WRITTEN, not just read back.

    A mutation battery removed the `sessionStorage.setItem` and the resume test
    above stayed green -- because that test plants the ticket itself and only
    exercises the restore path. This exercises the park path.

    Deterministic by aborting the event stream at the network layer: the run
    starts server-side, the browser never gets a stream, and `follow` takes its
    error branch. The ticket must survive that, because a lost stream is not a
    lost run -- it is still going, and still spending.
    """
    from playwright.sync_api import sync_playwright

    base, token = served

    with sync_playwright() as play:
        browser = play.chromium.launch()
        try:
            page = browser.new_page()
            page.route("**/api/runs/*/events", lambda route: route.abort())
            page.goto(f"{base}/#t={token}", wait_until="domcontentloaded")
            page.wait_for_selector("nav button", timeout=60_000)
            page.get_by_role("button", name="run", exact=True).click()
            page.get_by_role("button", name="Start a run").click()

            page.wait_for_function(
                "() => sessionStorage.getItem('whetstone.run.ticket') !== null",
                timeout=60_000,
            )
            parked = page.evaluate(
                "() => sessionStorage.getItem('whetstone.run.ticket')"
            )
            assert parked and len(parked) == 32
        finally:
            browser.close()
