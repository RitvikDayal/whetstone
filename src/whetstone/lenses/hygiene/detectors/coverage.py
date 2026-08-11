"""Flag test coverage below a configured floor.

Reads an existing coverage.xml. Whetstone does not run your test suite to
produce one -- that is `doctor`'s job and the user's choice.
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
        floor_raw = ctx.lens_options.get("coverage_floor", DEFAULT_FLOOR)
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

        artifact = self._find_artifact(ctx.project_root)
        if artifact is None:
            ctx.skip(
                "hygiene/coverage: no coverage.xml found "
                f"(looked in {', '.join(_ARTIFACTS)}). "
                "Generate one with your test runner to enable this check."
            )
            return

        try:
            rate = float(ElementTree.parse(artifact).getroot().attrib["line-rate"])
        except (ElementTree.ParseError, KeyError, ValueError) as exc:
            ctx.skip(f"hygiene/coverage: {artifact.name} is unreadable ({exc}).")
            return

        measured = round(rate * 100, 2)
        if measured >= floor:
            return

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
