"""The rendered-ui lens: drive, capture, the geometry, and the second pass.

WHAT IS FAKED AND WHAT IS NOT. The provider is faked, because the drive stage's
job is to be judged rather than to be clever. The BROWSER IS REAL wherever a
number matters: an overlap measured against a stub is a test of the stub. The
tests that need a real Chromium are marked, and they fail rather than skip
wherever CI is set, for the reason `sandbox_image` gives.

THAT LAST SENTENCE USED TO BE FALSE HERE. This module declared its own
`_needs_browser`, which skipped on every platform, and the fail-not-skip
guarantee held only because `test_browser.py` raised at import time and failed
collection for the whole session. The claim was made in one file and enforced
in another, so moving the guard would have left the six marked tests below
skipping silently on a green leg. Both now come from `tests/_browser.py`.
"""

from __future__ import annotations

from pathlib import Path

import pytest

# `tests/` is on the path for both test directories; see `tests/conftest.py`.
from _browser import Server, needs_browser
from whetstone.lenses.base import EvidenceKind, LensPack, LensScope, RunContext
from whetstone.lenses.rendered_ui.browser import Box, Origin
from whetstone.lenses.rendered_ui.capture import (
    DEFAULT_MIN_OVERLAP_PX,
    _agrees,
    capture,
)
from whetstone.lenses.rendered_ui.drive import Check, drive
from whetstone.lenses.rendered_ui.pack import RenderedUiPack
from whetstone.provider.base import StageResult, Usage

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


def test_the_pack_is_file_scoped():
    """Settled by the first real run, not by taste. The runner resolves no file
    list at all when nothing enabled is file-scoped, so a project-scoped
    rendered-ui got "(no files in scope)" and was asked to read markup it could
    not see."""
    assert RenderedUiPack().scope is LensScope.file


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


@needs_browser
def test_a_real_overlap_is_measured_and_reported(tmp_path):
    (tmp_path / "index.html").write_text(_OVERLAPPING, encoding="utf-8")
    shots = tmp_path / "shots"
    shots.mkdir()
    check = Check("/index.html", "#buy", "#badge", "absolutely positioned")
    with Server(tmp_path) as base:
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


@needs_browser
def test_a_clean_page_produces_nothing(tmp_path):
    """The half of the definition of done that is easy to forget."""
    (tmp_path / "index.html").write_text(_CLEAN_PAGE, encoding="utf-8")
    shots = tmp_path / "shots"
    shots.mkdir()
    check = Check("/index.html", "#buy", "#badge", "worth checking")
    with Server(tmp_path) as base:
        result = capture(Origin.parse(base), (check,), ((1280, 800),), shots)
    assert result.overlaps == ()
    assert result.skips == ()


@needs_browser
def test_a_selector_matching_nothing_is_reported_and_is_not_a_finding(tmp_path):
    """An absent element and one collapsed to zero size are different facts."""
    (tmp_path / "index.html").write_text(_OVERLAPPING, encoding="utf-8")
    shots = tmp_path / "shots"
    shots.mkdir()
    check = Check("/index.html", "#buy", "#nope", "worth checking")
    with Server(tmp_path) as base:
        result = capture(Origin.parse(base), (check,), ((1280, 800),), shots)
    assert result.overlaps == ()
    assert len(result.skips) == 1
    assert "matched no element" in result.skips[0]


@needs_browser
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
    with Server(tmp_path) as base:
        result = capture(
            Origin.parse(base),
            (check,),
            ((1280, 800),),
            shots,
            min_overlap_px=DEFAULT_MIN_OVERLAP_PX,
        )
    assert result.overlaps == ()


@needs_browser
def test_the_candidate_carries_capture_evidence_and_a_replayable_script(tmp_path):
    """`EvidenceKind.capture` was in the contract from M0, written before any
    browser code existed -- 'a screenshot plus replayable navigation'."""
    (tmp_path / "index.html").write_text(_OVERLAPPING, encoding="utf-8")
    shots = tmp_path / "shots"
    shots.mkdir()
    check = Check("/index.html", "#buy", "#badge", "absolutely positioned")
    with Server(tmp_path) as base:
        origin = Origin.parse(base)
        result = capture(origin, (check,), ((1280, 800),), shots)

    candidate = RenderedUiPack()._candidate(result.overlaps[0], origin)
    assert candidate.evidence.kind is EvidenceKind.capture
    replay = candidate.evidence.data["replay"]
    assert replay["measure"] == ["#buy", "#badge"]
    # THE FULL URL, not a bare path. A consumer following the replay had no host
    # or port to navigate to, so the evidence was not replayable -- which is the
    # one thing invariant 5 requires of it.
    assert replay["url"] == f"{origin}/index.html"
    assert replay["origin"] == str(origin)
    assert replay["route"] == "/index.html"
    assert candidate.evidence.data["viewport"] == [1280, 800]
    assert candidate.evidence.artifacts
    # The subject carries the viewport: the same pair at 360px is a different
    # finding, and a subject without the width would dedupe them into one.
    assert candidate.subject == "/index.html@1280x800"


# --- the bounds are enforced, not merely asked for -----------------------------------


def test_more_checks_than_the_cap_are_truncated_with_a_reason(tmp_path):
    """The schema permits 12 and the configured cap may be lower, so a bound the
    caller believed it set was one nothing enforced. Each surplus check costs two
    real renders per viewport."""
    provider = _FakeProvider(
        {
            "checks": [
                {"route": f"/p{i}", "selector_a": "#a", "selector_b": "#b",
                 "why": "x"}
                for i in range(9)
            ],
            "notes": None,
        }
    )
    result = drive(_ctx(tmp_path, max_checks=3), provider, _origin(), ((1280, 800),))
    assert len(result.checks) == 3
    assert any("were not measured" in s for s in result.skips)


def test_a_non_string_notes_value_is_reported_not_raised(tmp_path):
    """The schema forbids it and this layer does not trust the schema. `3` would
    reach `.strip()` and end the run with an AttributeError."""
    provider = _FakeProvider({"checks": [], "notes": 3})
    result = drive(_ctx(tmp_path), provider, _origin(), ((1280, 800),))
    assert result.checks == ()
    assert any("rather than text" in s for s in result.skips)


def test_a_non_list_checks_value_is_reported_not_raised(tmp_path):
    provider = _FakeProvider({"checks": {"route": "/"}, "notes": None})
    result = drive(_ctx(tmp_path), provider, _origin(), ((1280, 800),))
    assert result.checks == ()
    assert any("rather than a list" in s for s in result.skips)


def test_both_malformed_fields_keep_both_reasons(tmp_path):
    """The early return built a fresh tuple and dropped the reason recorded two
    lines above it -- a path that declines to do work and then discards its own
    explanation on the way out."""
    provider = _FakeProvider({"checks": {}, "notes": 3})
    result = drive(_ctx(tmp_path), provider, _origin(), ((1280, 800),))
    assert result.checks == ()
    assert any("rather than text" in s for s in result.skips), (
        "the notes reason was lost when checks was also malformed"
    )
    assert any("rather than a list" in s for s in result.skips)


def test_a_boolean_max_checks_falls_back_rather_than_capping_at_one(tmp_path):
    """`bool` is an `int` subclass, so `max_checks: true` was a cap of ONE that
    passed every type check. The third option in this lens bitten by it."""
    from whetstone.lenses.rendered_ui.drive import _DEFAULT_MAX_CHECKS, _max_checks

    assert _max_checks(_ctx(tmp_path, max_checks=True))[0] == _DEFAULT_MAX_CHECKS
    assert _max_checks(_ctx(tmp_path, max_checks=3)) == (3, None)
    # ABSENT IS NOT REFUSED. The default arrives through the same `.get()` and
    # must not manufacture a reason -- every run would carry one.
    assert _max_checks(_ctx(tmp_path)) == (_DEFAULT_MAX_CHECKS, None)


@pytest.mark.parametrize("configured", ["20", True, 0, -1, None, 2.5])
def test_a_refused_max_checks_tells_the_user_it_was_refused(tmp_path, configured):
    """`max_checks: "20"` means somebody wanted 20 and got 6. Falling back is
    declining to do work the caller asked for, and a run that quietly measured
    less than the configured surface reads as clean."""
    provider = _FakeProvider({"checks": [], "notes": None})
    result = drive(
        _ctx(tmp_path, max_checks=configured), provider, _origin(), ((1280, 800),)
    )
    assert any("max_checks" in s and "fell back" in s for s in result.skips), (
        f"a refused cap of {configured!r} left no reason the user can read"
    )


def test_a_refused_max_checks_survives_an_early_return(tmp_path):
    """The reason is recorded before the request and every early return has to
    carry it out. Building a fresh tuple on the way out is the same defect
    `test_both_malformed_fields_keep_both_reasons` pins one layer down."""
    provider = _FakeProvider({"checks": []}, denials=("Write",))
    result = drive(
        _ctx(tmp_path, max_checks="20"), provider, _origin(), ((1280, 800),)
    )
    assert result.checks == ()
    assert any("was refused Write" in s for s in result.skips)
    assert any("max_checks" in s for s in result.skips), (
        "the denial discarded the reason the cap was refused"
    )


def test_the_prompt_asks_for_the_cap_that_is_actually_enforced(tmp_path):
    """One cap, computed once. Two call sites reading `options` independently is
    how a prompt asks for 20 while truncation enforces 6."""
    provider = _FakeProvider({"checks": [], "notes": None})
    drive(_ctx(tmp_path, max_checks="20"), provider, _origin(), ((1280, 800),))
    prompt = provider.requests[0].prompt
    assert "Return at most 6 checks" in prompt
    assert "Return at most 20 checks" not in prompt


def test_a_screenshot_path_escaping_its_directory_is_refused(monkeypatch, tmp_path):
    """Containment asserted rather than argued. Nothing untrusted reaches the
    name today, which is precisely the claim that stops being true later."""
    from whetstone.lenses.rendered_ui import capture as capture_module

    check, _calls, fake = _fixed([500.0, 500.0], tmp_path)
    monkeypatch.setattr(capture_module, "measure_one", fake)
    monkeypatch.setattr(capture_module, "_inside", lambda root, candidate: False)
    result = capture_module.capture(_origin(), (check,), ((1280, 800),), tmp_path)
    assert result.overlaps == ()
    assert any("escaped" in s for s in result.skips)


def test_inside_rejects_a_sibling_that_shares_the_prefix(tmp_path):
    """The stubbed test above proves `capture()` reacts to a False. It cannot
    prove `_inside` ever returns one, and this is the case it exists for:
    `shots-elsewhere` starts with `shots`, which is exactly the prefix-matching
    defect the write barrier already refuses."""
    from whetstone.lenses.rendered_ui.capture import _inside

    root = tmp_path / "shots"
    root.mkdir()
    sibling = tmp_path / "shots-elsewhere"
    sibling.mkdir()

    assert _inside(root, root / "a.png") is True
    assert _inside(root, root / "nested" / "a.png") is True
    assert _inside(root, sibling / "a.png") is False


def test_inside_rejects_a_traversal_out_of_the_root(tmp_path):
    from whetstone.lenses.rendered_ui.capture import _inside

    root = tmp_path / "shots"
    root.mkdir()

    assert _inside(root, root / ".." / "a.png") is False
    assert _inside(root, root / "nested" / ".." / ".." / "a.png") is False
    assert _inside(root, tmp_path / "a.png") is False


def test_inside_follows_a_symlink_out_of_the_root(tmp_path):
    """`resolve()` is why this helper exists rather than a string compare. A
    link whose NAME is inside the root and whose TARGET is not must fail, or
    containment is a claim about spelling."""
    from whetstone.lenses.rendered_ui.capture import _inside

    root = tmp_path / "shots"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()

    link = root / "escape"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover
        # Windows needs Developer Mode or elevation for this. Skipped rather
        # than silently passing: a containment test that cannot make the link
        # has not tested containment.
        pytest.skip(f"cannot create a directory symlink here: {exc}")

    assert _inside(root, link / "a.png") is False


def test_an_empty_provider_name_is_refused_rather_than_defaulted(tmp_path):
    """`ModelConfig.provider` neither rejects nor normalises "", so `or` turned
    a typo in a config file into "the default is fine"."""
    from whetstone.errors import WhetstoneError

    pack = RenderedUiPack(provider_name="")
    with pytest.raises(WhetstoneError):
        pack._resolve_provider()


@pytest.mark.parametrize(
    "declared",
    [[[True, True]], [[0, 800]], [["1280", "800"]], [[1280]], "1280x800", []],
    ids=["bools", "zero", "strings", "one-number", "not-a-list", "empty"],
)
def test_an_unusable_viewport_is_never_silently_substituted(tmp_path, declared):
    """A silently substituted default means the lens measured under settings the
    user did not declare, and the report then reads as a result about the
    declared configuration.

    `bools` is the sharp one: `isinstance(True, int)` is true, so `[[true, true]]`
    measured a 1x1 page without a word about it."""
    from whetstone.lenses.rendered_ui.pack import _viewports

    ctx = _ctx(tmp_path, viewports=declared)
    assert _viewports(ctx) == ((1280, 800),)
    assert ctx.skips, "substituting a default without saying so is the defect"


def test_an_unusable_threshold_is_never_silently_substituted(tmp_path):
    from whetstone.lenses.rendered_ui.pack import _float_option

    ctx = _ctx(tmp_path, min_overlap_px=True)
    assert _float_option(ctx, "min_overlap_px", 4.0) == 4.0
    assert any("min_overlap_px" in s for s in ctx.skips)


def test_a_declared_threshold_is_used_without_complaint(tmp_path):
    """The counterweight. A check that fires on the good case teaches people to
    ignore it."""
    from whetstone.lenses.rendered_ui.pack import _float_option

    ctx = _ctx(tmp_path, min_overlap_px=12)
    assert _float_option(ctx, "min_overlap_px", 4.0) == 12.0
    assert ctx.skips == ()


# --- a discarded check leaves no evidence behind -------------------------------------


def test_a_discarded_check_removes_its_screenshot(monkeypatch, tmp_path):
    """An orphan PNG makes the one image that belongs to a finding
    indistinguishable from the ones that do not."""
    from whetstone.lenses.rendered_ui import capture as capture_module

    check, calls, fake = _fixed([500.0, 0.0], tmp_path)

    def writing(origin, chk, viewport, shot_path):
        if shot_path is not None:
            shot_path.write_bytes(b"png")
        return fake(origin, chk, viewport, shot_path)

    monkeypatch.setattr(capture_module, "measure_one", writing)
    result = capture_module.capture(_origin(), (check,), ((1280, 800),), tmp_path)
    assert result.overlaps == ()
    assert list(tmp_path.glob("*.png")) == []


def test_a_finding_whose_screenshot_never_landed_cites_no_artifact(
    monkeypatch, tmp_path
):
    """Evidence pointing at a nonexistent image is worse than evidence with
    none: the first looks checkable and is not."""
    from whetstone.lenses.rendered_ui import capture as capture_module

    check, _calls, fake = _fixed([500.0, 500.0], tmp_path)
    monkeypatch.setattr(capture_module, "measure_one", fake)
    result = capture_module.capture(_origin(), (check,), ((1280, 800),), tmp_path)
    assert result.overlaps[0].screenshot is None
    assert any("no screenshot reached" in s for s in result.skips)
    candidate = RenderedUiPack()._candidate(result.overlaps[0], _origin())
    assert candidate.evidence.artifacts == ()


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
