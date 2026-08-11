"""Flag test coverage below a configured floor.

Reads an existing coverage.xml. Whetstone never runs your test suite to produce
one: that is your test runner's job, invoked by you. (`doctor` re-verifies the
config against reality -- see cli.py -- it does not generate artifacts.)

The floor comes from `lenses.hygiene.options.coverage_floor`. It is read
through `RunContext.options`, not from the top level of `lens_options`, because
the top level holds only the spine's own typed keys; see `LensConfig.options`.
"""

from __future__ import annotations

import xml.etree.ElementTree as ElementTree
from collections.abc import Iterator
from pathlib import Path

from ...base import Candidate, Evidence, EvidenceKind, RunContext, Severity

DEFAULT_FLOOR = 60
_ARTIFACTS = ("coverage.xml", "reports/coverage.xml")


class CoverageDetector:
    id = "coverage"

    def detect(self, ctx: RunContext) -> Iterator[Candidate]:
        floor_raw = ctx.options.get("coverage_floor", DEFAULT_FLOOR)
        if isinstance(floor_raw, bool):
            # isinstance(True, int) is True and float(True) == 1.0 -- reject
            # before the numeric conversion would silently accept it. A
            # config saying `coverage_floor: true` almost certainly meant to
            # enable something, not to set a 1% floor.
            ctx.skip(
                "hygiene/coverage: coverage_floor option is a bool "
                f"({floor_raw!r}), not a percentage; coverage was not evaluated."
            )
            return

        try:
            # Deliberately not int()-truncated: int(59.9) == 59 would loosen a
            # fractional floor and let a real regression through silently. A
            # floor is only safe to round in the strict direction, and the
            # simplest strict choice is to not round at all -- keep whatever
            # precision the option was given.
            floor = float(floor_raw)
        except (TypeError, ValueError):
            ctx.skip(
                "hygiene/coverage: coverage_floor option is not a number "
                f"({floor_raw!r}); coverage was not evaluated."
            )
            return

        # 0 (or negative) can never fail -- it isn't a floor, it's the check
        # turned off, and a detector that runs and reports nothing looks
        # exactly like a clean project. 100 is kept as the legitimate
        # "require full coverage" value; only the boundary at 0 is excluded
        # because it is the value that can never produce a finding.
        if not (0 < floor <= 100):
            ctx.skip(
                "hygiene/coverage: coverage_floor option is out of range "
                f"({floor_raw!r}); must be > 0 and <= 100. Coverage was not "
                "evaluated."
            )
            return

        artifact = self._find_artifact(ctx.project_root)
        if artifact is None:
            ctx.skip(
                "hygiene/coverage: no coverage.xml found "
                f"(looked in {', '.join(_ARTIFACTS)}). "
                "Generate one with your test runner to enable this check."
            )
            return

        try:
            # OSError too: `_find_artifact` proved the path was a file, and the
            # open happens after. Deleted in between, locked by the test runner
            # still writing it, or unreadable -- ElementTree raises OSError, and
            # without this the user reads pack.py's generic "raised
            # PermissionError" instead of the detector's own sentence.
            rate = float(ElementTree.parse(artifact).getroot().attrib["line-rate"])
        except (OSError, ElementTree.ParseError, KeyError, ValueError) as exc:
            ctx.skip(f"hygiene/coverage: {artifact.name} is unreadable ({exc}).")
            return

        # Compared unrounded. Rounding first moves the measurement in the
        # loosening direction: line-rate 0.599999 becomes 60.0 and clears a
        # floor of 60 that real coverage is below. The floor above is
        # deliberately not truncated for the same reason; rounding the other
        # side of the comparison gave the loosening back. The rounded value is
        # for display only.
        exact = rate * 100
        if exact >= floor:
            return
        measured = round(exact, 2)

        floor_display = f"{floor:g}"  # 60.0 -> "60", 59.9 -> "59.9"
        yield Candidate(
            lens="hygiene",
            rule_id="coverage-below-floor",
            subject=artifact.name,
            title=f"Line coverage is {measured}%, below the {floor_display}% floor",
            detail=(
                f"{artifact} reports {measured}% line coverage against a configured "
                f"floor of {floor_display}%. Raise the floor deliberately or add "
                "tests; a floor nobody meets is a floor nobody reads."
            ),
            severity=Severity.medium,
            evidence=Evidence(
                kind=EvidenceKind.metric,
                summary=f"line-rate {measured}% < floor {floor_display}%",
                data={"measured": measured, "floor": floor, "source": artifact.name},
                artifacts=(str(artifact),),
            ),
        )

    @staticmethod
    def _find_artifact(root: Path) -> Path | None:
        for relative in _ARTIFACTS:
            candidate = root / relative
            if candidate.is_file():
                return candidate
        return None
