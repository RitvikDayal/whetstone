"""Capture, verify-geometry and the second pass -- all three done by the CONTROLLER.

INVARIANT 3, IN THE SHAPE THIS LENS TAKES. "These two controls overlap" is a
candidate. Two bounding boxes whose intersection is non-empty is evidence. Every
number in a finding from this lens was measured here, by a real browser, from a
page this process navigated. Nothing arrives from a model payload.

THE SECOND PASS IS NOT A RETRY. Animations, web fonts and async render make a
single measurement a coin flip -- the design names this as the browser lens's
flakiness risk, and `_settle()` suppressing its own timeouts is why it is a real
one rather than a theoretical one. The page is rendered twice, in two separate
browser contexts, and a finding that does not survive both is DROPPED with the
reason recorded. It is the same standard `reproduce.py` meets by running the
artifact rather than believing it.

WHY A TOLERANCE, AND WHY IT IS NOT ZERO. Two renders of the same correct page
differ by fractions of a pixel: subpixel layout, font hinting, and scrollbar
presence all move a box slightly. Requiring the two passes to agree exactly
would drop real findings for noise; allowing them to disagree freely would let
a coin flip through. The tolerance is on the OVERLAP AREA, and it is expressed
as a fraction of the smaller measurement so it scales with the size of the thing
being measured.

WHY A FLOOR ON THE AREA. A one-pixel intersection is what a correct page
produces when two elements abut and the layout engine rounds. Reporting it is
how a lens earns the reputation of crying wolf, which costs more than the
findings it would have gained.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import NamedTuple

from .browser import Box, BrowserError, Origin, rendered
from .drive import Check

# Square CSS pixels below which an intersection is not a finding. Two abutting
# elements round into each other by well under this.
DEFAULT_MIN_OVERLAP_PX = 4.0

# How far the two passes may disagree on the overlap area, as a fraction of the
# smaller of the two. Subpixel layout and font hinting move a box by a fraction
# of a pixel; a real overlap of any size survives this comfortably.
DEFAULT_STABILITY_TOLERANCE = 0.25


class Measurement(NamedTuple):
    """What one pass measured for one check, at one viewport."""

    check: Check
    viewport: tuple[int, int]
    box_a: Box | None
    box_b: Box | None
    overlap_px: float
    screenshot: Path | None
    # Why the image was withdrawn, when it was. Defaulted so a Measurement
    # that kept its screenshot -- every ordinary one -- reads the same as it
    # always did.
    shot_withdrawn: str | None = None

    @property
    def missing(self) -> tuple[str, ...]:
        """Selectors that matched nothing. Not an error and not a finding."""
        absent = []
        if self.box_a is None:
            absent.append(self.check.selector_a)
        if self.box_b is None:
            absent.append(self.check.selector_b)
        return tuple(absent)


class Overlap(NamedTuple):
    """A finding that survived both passes, with both measurements kept."""

    check: Check
    viewport: tuple[int, int]
    first: Measurement
    second: Measurement
    screenshot: Path | None

    @property
    def overlap_px(self) -> float:
        """The SMALLER of the two passes. A finding claims the least it measured.

        Reporting the larger would let the noisier pass set the number, and the
        claim would then outrun the weaker of the two observations behind it.
        """
        return min(self.first.overlap_px, self.second.overlap_px)


class CaptureResult(NamedTuple):
    overlaps: tuple[Overlap, ...]
    skips: tuple[str, ...]


def measure_one(
    origin: Origin,
    check: Check,
    viewport: tuple[int, int],
    shot_path: Path | None,
) -> Measurement:
    """Render the route once and measure both selectors.

    THE SCREENSHOT IS BRACKETED BY THE GEOMETRY READS, and this used to be an
    ordering argument instead. Capturing before reading was defended here on
    the grounds that reading first would let a late render change the page
    between the evidence and the measurement -- true, and the mirror of it is
    equally true: a font landing or an animation settling AFTER the PNG is
    written leaves an image that does not show the rectangles the numbers
    describe. Ordering cannot close that gap from either side. Only measuring
    it can.

    So both boxes are read, the image is taken, and both are read again. If
    either rectangle moved, the numbers still stand -- they are a real
    measurement of a real moment -- but the image no longer evidences them, so
    it is deleted and the reason travels with the Measurement. An image that
    shows a different layout than the finding claims is worse than no image:
    it looks checkable and is not.
    """
    url = f"{origin}{check.route}"
    with rendered(url, viewport=viewport) as page:
        box_a = page.box(check.selector_a)
        box_b = page.box(check.selector_b)
        if shot_path is not None:
            page.screenshot(shot_path)
            after_a = page.box(check.selector_a)
            after_b = page.box(check.selector_b)
        else:
            after_a, after_b = box_a, box_b

    withdrawn: str | None = None
    if shot_path is not None and (after_a != box_a or after_b != box_b):
        _discard_shot(shot_path)
        withdrawn = (
            f"the layout moved while {check.route} was being captured, so the "
            f"screenshot does not show the geometry that was measured and was "
            f"discarded. The measurement itself stands."
        )
        shot_path = None

    overlap = (
        box_a.intersection_area(box_b)
        if box_a is not None and box_b is not None
        else 0.0
    )
    return Measurement(
        check, viewport, box_a, box_b, overlap, shot_path, withdrawn
    )


def _agrees(first: float, second: float, tolerance: float) -> bool:
    """Whether two overlap areas are the same measurement twice.

    Relative to the SMALLER of the two, so the tolerance scales with the size of
    what is being measured rather than being generous about small overlaps and
    stingy about large ones. Both being zero counts as agreement: the page was
    stably fine.
    """
    if first <= 0.0 and second <= 0.0:
        return True
    smaller = min(first, second)
    if smaller <= 0.0:
        # One pass saw an overlap and the other saw none. That is precisely the
        # coin flip the second pass exists to catch, and no tolerance should
        # absorb it.
        return False
    return abs(first - second) / smaller <= tolerance


def capture(
    origin: Origin,
    checks: tuple[Check, ...],
    viewports: tuple[tuple[int, int], ...],
    shots_dir: Path,
    *,
    min_overlap_px: float = DEFAULT_MIN_OVERLAP_PX,
    tolerance: float = DEFAULT_STABILITY_TOLERANCE,
) -> CaptureResult:
    """Measure every check at every viewport, twice, and keep what repeats."""
    overlaps: list[Overlap] = []
    skips: list[str] = []

    for index, check in enumerate(checks):
        for position, viewport in enumerate(viewports):
            width, height = viewport
            # THE VIEWPORT'S POSITION, not only its size. Nothing deduplicates
            # `viewports`, so `[[1280, 800], [1280, 800]]` gave two iterations
            # the SAME path: the first could record an Overlap citing that
            # file, and the second could then discard for any reason and unlink
            # it. The finding survives in the report pointing at an image that
            # is gone, which is the exact failure the containment comment below
            # and `_discard_shot` are both written to prevent. Position rather
            # than deduplication, because dropping the second viewport would be
            # declining work the user configured, and that needs its own reason.
            stem = f"{index:02d}-{position:02d}-{width}x{height}"
            shot = shots_dir / f"{stem}.png"
            # CONTAINMENT, asserted rather than argued. Every component of this
            # name is a loop index or a viewport integer, so nothing a model or
            # a config wrote reaches it -- but "no untrusted input reaches this
            # path" is exactly the kind of claim that stops being true when
            # somebody later builds the name from a route. The check costs
            # nothing and turns the argument into a guarantee. It is the only
            # place a path is derived, so the screenshot write and the unlink
            # below are both covered by it.
            if not _inside(shots_dir, shot):
                skips.append(
                    f"rendered-ui [{check.route} @ {width}x{height}]: the "
                    f"screenshot path escaped {shots_dir}, so nothing was "
                    f"captured or measured for this check."
                )
                continue
            try:
                first = measure_one(origin, check, viewport, shot)
                # A SEPARATE CONTEXT, not a second read of the same page. The
                # whole question is whether rendering it again produces the same
                # layout, and re-reading one page answers a different one.
                second = measure_one(origin, check, viewport, None)
            except BrowserError as exc:
                _discard_shot(shot)
                skips.append(
                    f"rendered-ui [{check.route} @ {width}x{height}]: could not "
                    f"measure {check.selector_a!r} against {check.selector_b!r}: "
                    f"{exc}"
                )
                continue

            absent = first.missing
            if absent:
                _discard_shot(shot)
                skips.append(
                    f"rendered-ui [{check.route} @ {width}x{height}]: "
                    f"{', '.join(repr(s) for s in absent)} matched no element, so "
                    f"nothing was measured. An absent element and one collapsed "
                    f"to zero size are different facts and this is the first."
                )
                continue

            # THE SECOND PASS TOO, and it needs its own branch rather than
            # riding on `_agrees`. A selector the second render could not find
            # scores 0.0, and where the FIRST pass also measured 0.0 the two
            # "agree" -- so a pair that was never measured twice fell through
            # to the zero branch below and was discarded as a clean page with
            # nothing said. A real overlap in the first pass is caught by
            # `_agrees` already, because 500 against 0 is a disagreement; this
            # is the case where zero hides a failure to look.
            if second.missing:
                _discard_shot(shot)
                skips.append(
                    f"rendered-ui [{check.route} @ {width}x{height}]: "
                    f"{', '.join(repr(s) for s in second.missing)} matched no "
                    f"element on the second render, so the pair was measured "
                    f"once rather than twice and nothing here reproduced."
                )
                continue

            if not _agrees(first.overlap_px, second.overlap_px, tolerance):
                _discard_shot(shot)
                skips.append(
                    f"rendered-ui [{check.route} @ {width}x{height}]: measured "
                    f"{first.overlap_px:.1f} and then {second.overlap_px:.1f} "
                    f"square pixels of overlap for the same two elements. It did "
                    f"not reproduce, so it is NOT reported."
                )
                continue

            smaller = min(first.overlap_px, second.overlap_px)
            if smaller <= 0.0:
                # ZERO IS NOT AN OVERLAP AT ANY FLOOR, and this is checked
                # BEFORE the configured one rather than through it. `0` is
                # finite, numeric and not negative, so `min_overlap_px: 0`
                # passes `_float_option` -- and then `smaller < min_overlap_px`
                # reads `0.0 < 0.0`, which is false. Every separated pair on a
                # correct page agreed with itself at zero and was reported as a
                # candidate overlapping by nought square pixels. Same direction
                # as the `nan` case: the lens INVENTING findings, which is the
                # one failure a tool built on evidence cannot have.
                #
                # Not a skip. The check ran, the page was fine, and calling
                # that work-not-done would be false.
                _discard_shot(shot)
                continue
            if smaller < min_overlap_px:
                _discard_shot(shot)
                skips.append(
                    f"rendered-ui [{check.route} @ {width}x{height}]: "
                    f"{smaller:.1f} square pixels of overlap is below the "
                    f"{min_overlap_px:.1f} floor and was not reported. Two "
                    f"abutting elements round into each other by about this "
                    f"much."
                )
                continue

            # THE FILE, NOT THE PATH WE ASKED FOR. A screenshot the driver never
            # wrote would otherwise be cited as an artifact, and evidence
            # pointing at a nonexistent image is worse than evidence with none:
            # the first looks checkable and is not.
            # `first.shot_withdrawn` FIRST, and not through `shot.exists()`.
            # `_discard_shot` suppresses OSError, so an unlink that failed --
            # a scanner or the indexer holding a transient handle on the file
            # Playwright just wrote, which is ordinary on Windows -- left the
            # image on disk, `exists()` answered True, and the finding cited a
            # PNG of a layout it did not measure with no reason recorded
            # anywhere. The withdrawal is a decision this code made; whether
            # the file went is the filesystem's opinion about it.
            if first.shot_withdrawn is not None or not shot.exists():
                # ONE MESSAGE, THE ACCURATE ONE. A shot withdrawn because the
                # layout moved is a different fact from one the driver never
                # wrote, and reporting the second when the first happened
                # sends the reader looking for a disk problem.
                skips.append(
                    f"rendered-ui [{check.route} @ {width}x{height}]: "
                    + (
                        first.shot_withdrawn
                        if first.shot_withdrawn is not None
                        else (
                            f"the overlap was measured but no screenshot reached "
                            f"{shot.name}, so it is reported without one rather "
                            f"than citing an image that is not there."
                        )
                    )
                )
                overlaps.append(Overlap(check, viewport, first, second, None))
                continue

            overlaps.append(Overlap(check, viewport, first, second, shot))

    return CaptureResult(tuple(overlaps), tuple(skips))


def _inside(root: Path, candidate: Path) -> bool:
    """Whether *candidate* resolves inside *root*.

    `resolve()` on both, and a real containment test rather than `startswith`:
    prefix matching on a path is the defect the write barrier already refuses,
    and `/state/shots-elsewhere` starts with `/state/shots`.
    """
    try:
        candidate.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        return False
    return True


def _discard_shot(shot: Path) -> None:
    """Remove the image of a check that is not being reported.

    A discarded check leaves an ORPHAN otherwise: the evidence directory fills
    with PNGs no finding references, and the one image that does belong to a
    finding is indistinguishable from the ones that do not. Failure to unlink is
    deliberately ignored -- a leftover file is untidy, and turning it into an
    exception would lose the whole run over housekeeping.
    """
    with contextlib.suppress(OSError):
        shot.unlink()
