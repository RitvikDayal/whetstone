"""The rendered-ui lens: drive, capture, the geometry, and the second pass.

WHAT IS FAKED AND WHAT IS NOT. The provider is faked, because the drive stage's
job is to be judged rather than to be clever. The BROWSER IS REAL wherever a
number matters: an overlap measured against a stub is a test of the stub. The
tests that need a real Chromium are marked, and they fail rather than skip on the
Linux CI legs, for the reason `sandbox_image` gives.
"""

from __future__ import annotations

import http.server
import threading
from pathlib import Path

import pytest

from whetstone.lenses.base import EvidenceKind, LensPack, LensScope, RunContext
from whetstone.lenses.rendered_ui.browser import Box, Origin, availability
from whetstone.lenses.rendered_ui.capture import (
    DEFAULT_MIN_OVERLAP_PX,
    _agrees,
    capture,
)
from whetstone.lenses.rendered_ui.drive import Check, drive
from whetstone.lenses.rendered_ui.pack import RenderedUiPack
from whetstone.provider.base import StageResult, Usage

_UNAVAILABLE = availability()
_needs_browser = pytest.mark.skipif(
    _UNAVAILABLE is not None, reason=_UNAVAILABLE or "browser available"
)

# Two controls that genuinely collide at 1280px: the badge is absolutely
# positioned over the button rather than beside it.
_OVERLAPPING = """<!doctype html><body style="margin:0">
<button id="buy" style="position:absolute;left:100px;top:100px;
  width:200px;height:50px">Buy now</button>
<span id="badge" style="position:absolute;left:250px;top:120px;
  width:100px;height:30px;background:red">SALE</span>
</body>"""

# The same page with the badge moved clear of the button.
_CLEAN_PAGE = """<!doctype html><body style="margin:0">
<button id="buy" style="position:absolute;left:100px;top:100px;
  width:200px;height:50px">Buy now</button>
<span id="badge" style="position:absolute;left:400px;top:120px;
  width:100px;height:30px;background:red">SALE</span>
</body>"""


class _Server:
    def __init__(self, root: Path) -> None:
        handler = type(
            "H",
            (http.server.SimpleHTTPRequestHandler,),
            {
                "__init__": lambda s, *a, **k: http.server.SimpleHTTPRequestHandler.__init__(  # noqa: E501
                    s, *a, directory=str(root), **k
                ),
                "log_message": lambda *a, **k: None,
            },
        )
        self._httpd = http.server.HTTPServer(("127.0.0.1", 0), handler)
        self.port = self._httpd.server_address[1]

    def __enter__(self) -> str:
        threading.Thread(target=self._httpd.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{self.port}"

    def __exit__(self, *_exc) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()


class _FakeProvider:
    """Returns one canned payload. The drive stage is judged, not trusted."""

    name = "fake"

    def __init__(self, data, *, mutation=None, denials=(), ok=True, error=None):
        self._data = data
        self._mutation = mutation
        self._denials = tuple(denials)
        self._ok = ok
        self._error = error
        self.requests = []

    def run_stage(self, request):
        self.requests.append(request)
        return StageResult(
            ok=self._ok,
            data=self._data,
            raw="",
            error=self._error,
            turns=3,
            denials=self._denials,
            mutation=self._mutation,
            usage=Usage(
                input_tokens=1,
                output_tokens=1,
                cost_usd=0.01,
                wall_seconds=0.1,
                source="test",
            ),
        )


def _ctx(tmp_path, **options) -> RunContext:
    return RunContext(
        project_root=tmp_path,
        state_root=tmp_path / "state",
        files=(Path("app.html"),),
        tier="deep",
        lens_options={"options": options},
        run_id="run-test",
    )


def _origin() -> Origin:
    return Origin.parse("http://127.0.0.1:3000/")


# --- the pack fits the contract that already existed --------------------------------


def test_the_pack_satisfies_the_lens_protocol_unchanged():
    """The whole question M2 asks. A second pack with a different evidence type
    either fits `LensPack` or the abstraction is wrong."""
    assert isinstance(RenderedUiPack(), LensPack)


def test_the_pack_is_project_scoped():
    """It reads a running app, not the files `boundaries.include` selects. A user
    who excluded a path and still gets a finding has been told something false by
    silence."""
    assert RenderedUiPack().scope is LensScope.project


def test_autonomy_stops_below_the_writer():
    """A measurement is not an executable proof, so this lens never opens a PR."""
    assert RenderedUiPack().max_autonomy == 1


# --- drive proposes, and is judged --------------------------------------------------


def test_drive_keeps_a_well_formed_check(tmp_path):
    provider = _FakeProvider(
        {
            "checks": [
                {
                    "route": "/",
                    "selector_a": "#buy",
                    "selector_b": "#badge",
                    "why": "the badge is absolutely positioned",
                }
            ],
            "notes": None,
        }
    )
    result = drive(_ctx(tmp_path), provider, _origin(), ((1280, 800),))
    assert result.checks == (
        Check("/", "#buy", "#badge", "the badge is absolutely positioned"),
    )
    assert result.skips == ()


@pytest.mark.parametrize(
    "route",
    ["http://evil.test/", "//evil.test/", "relative", ""],
    ids=["absolute-elsewhere", "protocol-relative", "not-a-path", "empty"],
)
def test_drive_discards_a_route_that_is_not_on_the_origin(tmp_path, route):
    """A model cannot steer the browser by proposing a route. Checked HERE as
    well as in the adapter: this one produces a reason, that one is the guard."""
    provider = _FakeProvider(
        {"checks": [{"route": route, "selector_a": "#a", "selector_b": "#b",
                     "why": "x"}], "notes": None}
    )
    result = drive(_ctx(tmp_path), provider, _origin(), ((1280, 800),))
    assert result.checks == ()
    assert len(result.skips) == 1
    assert "discarded a check" in result.skips[0]


def test_drive_discards_a_check_of_an_element_against_itself(tmp_path):
    """An element always intersects itself, so this can only produce a false
    finding."""
    provider = _FakeProvider(
        {"checks": [{"route": "/", "selector_a": "#buy", "selector_b": " #buy ",
                     "why": "x"}], "notes": None}
    )
    result = drive(_ctx(tmp_path), provider, _origin(), ((1280, 800),))
    assert result.checks == ()
    assert "always intersects itself" in result.skips[0]


def test_a_drive_stage_that_wrote_has_its_proposals_discarded(tmp_path):
    """A read-only stage that mutated the worktree is not a stage whose
    proposals mean anything, whatever its payload looked like."""
    provider = _FakeProvider(
        {"checks": [{"route": "/", "selector_a": "#a", "selector_b": "#b",
                     "why": "x"}], "notes": None},
        mutation="app.html was modified",
    )
    result = drive(_ctx(tmp_path), provider, _origin(), ((1280, 800),))
    assert result.checks == ()
    assert "modified the worktree" in result.skips[0]


def test_a_drive_stage_that_was_refused_a_tool_is_discarded(tmp_path):
    provider = _FakeProvider(
        {"checks": [{"route": "/", "selector_a": "#a", "selector_b": "#b",
                     "why": "x"}], "notes": None},
        denials=("Read",),
    )
    result = drive(_ctx(tmp_path), provider, _origin(), ((1280, 800),))
    assert result.checks == ()
    assert "refused Read" in result.skips[0]


def test_an_empty_proposal_carries_its_reason_as_a_note_not_a_skip(tmp_path):
    """Finding nothing is an answer. A skip means the stage did not run."""
    provider = _FakeProvider({"checks": [], "notes": "no absolute positioning"})
    result = drive(_ctx(tmp_path), provider, _origin(), ((1280, 800),))
    assert result.checks == ()
    assert result.skips == ()
    assert result.notes == ("drive: no absolute positioning",)


def test_the_drive_stage_is_read_only(tmp_path):
    """It reads markup and proposes selectors. The browser is the controller's."""
    provider = _FakeProvider({"checks": [], "notes": "nothing"})
    drive(_ctx(tmp_path), provider, _origin(), ((1280, 800),))
    permissions = provider.requests[0].permissions
    assert "Write" not in permissions.available_tools
    assert "Edit" not in permissions.available_tools
    assert "Bash" not in permissions.available_tools
    assert permissions.write_root is None


# --- the geometry is arithmetic, and the controller does it -------------------------


def test_two_boxes_that_miss_on_both_axes_do_not_overlap():
    """The diagonal case: dx < 0 and dy < 0 multiply to a POSITIVE number, so
    without the guard the two most unrelated controls report the largest
    overlap."""
    assert Box(0, 0, 10, 10).intersection_area(Box(50, 50, 10, 10)) == 0.0


def test_agreement_is_relative_to_the_smaller_measurement():
    assert _agrees(100.0, 110.0, 0.25)
    assert not _agrees(100.0, 200.0, 0.25)


def test_an_overlap_in_one_pass_and_none_in_the_other_never_agrees():
    """The exact coin flip the second pass exists to catch. No tolerance may
    absorb it."""
    assert not _agrees(0.0, 40.0, 0.99)
    assert not _agrees(40.0, 0.0, 0.99)


def test_two_stable_zeroes_agree():
    """The page was stably fine, which is agreement rather than a failure."""
    assert _agrees(0.0, 0.0, 0.0)


# --- the second pass, proven to exist -----------------------------------------------


def _fixed(overlaps, tmp_path):
    """A `measure_one` that returns the given overlap areas in order.

    Fakes the MEASUREMENT rather than the browser, because what is under test
    here is whether `capture` renders twice and compares -- not what Chromium
    does with a stylesheet.
    """
    check = Check("/", "#a", "#b", "why")
    calls = []

    def fake(origin, chk, viewport, shot_path):
        from whetstone.lenses.rendered_ui.capture import Measurement

        area = overlaps[len(calls)]
        calls.append((chk, viewport, shot_path))
        return Measurement(
            chk, viewport, Box(0, 0, 10, 10), Box(0, 0, 10, 10), area, shot_path
        )

    return check, calls, fake


def test_capture_renders_twice(monkeypatch, tmp_path):
    """The second pass exists. Deleting it, or reusing the first measurement,
    turns this red -- which is the whole of Task 5."""
    from whetstone.lenses.rendered_ui import capture as capture_module

    check, calls, fake = _fixed([500.0, 500.0], tmp_path)
    monkeypatch.setattr(capture_module, "measure_one", fake)
    result = capture_module.capture(
        _origin(), (check,), ((1280, 800),), tmp_path
    )
    assert len(calls) == 2, "the page must be measured twice, in two renders"
    assert len(result.overlaps) == 1


def test_the_second_pass_is_a_separate_render_not_a_second_read(
    monkeypatch, tmp_path
):
    """Only the FIRST pass writes a screenshot. The second is a fresh render
    whose job is to disagree, and a second image of the same page proves
    nothing."""
    from whetstone.lenses.rendered_ui import capture as capture_module

    check, calls, fake = _fixed([500.0, 500.0], tmp_path)
    monkeypatch.setattr(capture_module, "measure_one", fake)
    capture_module.capture(_origin(), (check,), ((1280, 800),), tmp_path)
    assert calls[0][2] is not None
    assert calls[1][2] is None


def test_an_overlap_that_does_not_reproduce_is_dropped_with_a_reason(
    monkeypatch, tmp_path
):
    """Invariant 4. Animations, fonts and async render make a single screenshot
    a coin flip, and a finding that flips is not reported."""
    from whetstone.lenses.rendered_ui import capture as capture_module

    check, _calls, fake = _fixed([500.0, 0.0], tmp_path)
    monkeypatch.setattr(capture_module, "measure_one", fake)
    result = capture_module.capture(
        _origin(), (check,), ((1280, 800),), tmp_path
    )
    assert result.overlaps == ()
    assert len(result.skips) == 1
    assert "did not reproduce" in result.skips[0]


def test_a_finding_claims_the_smaller_of_its_two_measurements(
    monkeypatch, tmp_path
):
    """Reporting the larger would let the noisier pass set the number, and the
    claim would outrun the weaker of the two observations behind it."""
    from whetstone.lenses.rendered_ui import capture as capture_module

    check, _calls, fake = _fixed([500.0, 460.0], tmp_path)
    monkeypatch.setattr(capture_module, "measure_one", fake)
    result = capture_module.capture(
        _origin(), (check,), ((1280, 800),), tmp_path
    )
    assert result.overlaps[0].overlap_px == 460.0


# --- against a real browser ---------------------------------------------------------


@_needs_browser
def test_a_real_overlap_is_measured_and_reported(tmp_path):
    (tmp_path / "index.html").write_text(_OVERLAPPING, encoding="utf-8")
    shots = tmp_path / "shots"
    shots.mkdir()
    check = Check("/index.html", "#buy", "#badge", "absolutely positioned")
    with _Server(tmp_path) as base:
        result = capture(
            Origin.parse(base), (check,), ((1280, 800),), shots
        )
    assert len(result.overlaps) == 1
    overlap = result.overlaps[0]
    # The button spans x 100-300, the badge x 250-350 and y 120-150 inside the
    # button's y 100-150. The intersection is 50 wide by 30 tall.
    assert overlap.overlap_px == pytest.approx(1500, abs=50)
    assert overlap.screenshot.exists()
    assert overlap.screenshot.stat().st_size > 0


@_needs_browser
def test_a_clean_page_produces_nothing(tmp_path):
    """The half of the definition of done that is easy to forget."""
    (tmp_path / "index.html").write_text(_CLEAN_PAGE, encoding="utf-8")
    shots = tmp_path / "shots"
    shots.mkdir()
    check = Check("/index.html", "#buy", "#badge", "worth checking")
    with _Server(tmp_path) as base:
        result = capture(Origin.parse(base), (check,), ((1280, 800),), shots)
    assert result.overlaps == ()
    assert result.skips == ()


@_needs_browser
def test_a_selector_matching_nothing_is_reported_and_is_not_a_finding(tmp_path):
    """An absent element and one collapsed to zero size are different facts."""
    (tmp_path / "index.html").write_text(_OVERLAPPING, encoding="utf-8")
    shots = tmp_path / "shots"
    shots.mkdir()
    check = Check("/index.html", "#buy", "#nope", "worth checking")
    with _Server(tmp_path) as base:
        result = capture(Origin.parse(base), (check,), ((1280, 800),), shots)
    assert result.overlaps == ()
    assert len(result.skips) == 1
    assert "matched no element" in result.skips[0]


@_needs_browser
def test_an_overlap_below_the_floor_is_not_reported(tmp_path):
    """A one-pixel intersection is what a correct page produces when two
    elements abut and the layout engine rounds."""
    (tmp_path / "index.html").write_text(
        '<!doctype html><body style="margin:0">'
        '<div id="a" style="position:absolute;left:0;top:0;width:100px;'
        'height:100px"></div>'
        '<div id="b" style="position:absolute;left:99.5px;top:0;width:100px;'
        'height:1px"></div></body>',
        encoding="utf-8",
    )
    shots = tmp_path / "shots"
    shots.mkdir()
    check = Check("/index.html", "#a", "#b", "abutting")
    with _Server(tmp_path) as base:
        result = capture(
            Origin.parse(base),
            (check,),
            ((1280, 800),),
            shots,
            min_overlap_px=DEFAULT_MIN_OVERLAP_PX,
        )
    assert result.overlaps == ()


@_needs_browser
def test_the_candidate_carries_capture_evidence_and_a_replayable_script(tmp_path):
    """`EvidenceKind.capture` was in the contract from M0, written before any
    browser code existed -- 'a screenshot plus replayable navigation'."""
    (tmp_path / "index.html").write_text(_OVERLAPPING, encoding="utf-8")
    shots = tmp_path / "shots"
    shots.mkdir()
    check = Check("/index.html", "#buy", "#badge", "absolutely positioned")
    with _Server(tmp_path) as base:
        result = capture(Origin.parse(base), (check,), ((1280, 800),), shots)

    candidate = RenderedUiPack()._candidate(result.overlaps[0])
    assert candidate.evidence.kind is EvidenceKind.capture
    assert candidate.evidence.data["replay"]["measure"] == ["#buy", "#badge"]
    assert candidate.evidence.data["viewport"] == [1280, 800]
    assert candidate.evidence.artifacts
    # The subject carries the viewport: the same pair at 360px is a different
    # finding, and a subject without the width would dedupe them into one.
    assert candidate.subject == "/index.html@1280x800"


# --- the pack declines loudly -------------------------------------------------------


def test_no_base_url_is_a_skip_with_a_reason(tmp_path):
    ctx = _ctx(tmp_path)
    assert RenderedUiPack(provider=_FakeProvider({}))._collect(ctx) == []
    assert any("base_url" in skip for skip in ctx.skips)


def test_the_quick_tier_says_why_it_did_not_run(tmp_path):
    ctx = RunContext(
        project_root=tmp_path,
        state_root=tmp_path / "state",
        files=(),
        tier="quick",
        lens_options={"options": {"base_url": "http://127.0.0.1:3000"}},
        run_id="run-test",
    )
    assert RenderedUiPack(provider=_FakeProvider({}))._collect(ctx) == []
    assert any("costs real money" in skip for skip in ctx.skips)
