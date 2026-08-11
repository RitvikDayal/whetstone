"""The hygiene lens pack."""

from __future__ import annotations

from collections.abc import Iterator

from ..base import Candidate, LensScope, RunContext
from .detectors.base import Detector
from .detectors.coverage import CoverageDetector
from .detectors.deps import DepsDetector

DETECTORS: tuple[Detector, ...] = (DepsDetector(), CoverageDetector())


class HygienePack:
    """Mechanical checks. No model calls, so every tier supports it."""

    name = "hygiene"
    max_autonomy = 3
    # Both detectors read paths they choose themselves -- coverage.xml, the
    # dependency manifest -- and never touch RunContext.files, so
    # boundaries.include/exclude do not narrow what this lens examines.
    # Declared so the runner can say so rather than letting a user assume their
    # `exclude` took effect.
    scope = LensScope.project

    def supports_tier(self, tier: str) -> bool:
        return True

    def run(self, ctx: RunContext) -> Iterator[Candidate]:
        only = ctx.lens_options.get("only")
        for detector in DETECTORS:
            if only is not None and detector.id not in only:
                ctx.skip(
                    f"hygiene/{detector.id}: not in `only` "
                    f"({', '.join(only)}); not run."
                )
                continue
            try:
                yield from detector.detect(ctx)
            except Exception as exc:  # noqa: BLE001 - one detector, one blast radius
                # Without this, `deps` raising took `coverage` with it: a
                # coverage finding one line from being produced disappeared,
                # the run row read `failed`, and nothing recorded that coverage
                # had never run. The exception type and message go into the
                # skip text on purpose -- a bug in our own detector has to stay
                # findable, not become an anonymous "something went wrong".
                #
                # GeneratorExit and KeyboardInterrupt derive from BaseException,
                # so a consumer closing this generator still propagates.
                ctx.skip(
                    f"hygiene/{detector.id}: raised {type(exc).__name__}: {exc}. "
                    "That detector did not finish, so any findings it would "
                    "have produced are missing from this run."
                )
