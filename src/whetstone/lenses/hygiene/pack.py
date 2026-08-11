"""The hygiene lens pack."""

from __future__ import annotations

from collections.abc import Iterator

from ..base import Candidate, RunContext
from .detectors.base import Detector
from .detectors.coverage import CoverageDetector
from .detectors.deps import DepsDetector

DETECTORS: tuple[Detector, ...] = (DepsDetector(), CoverageDetector())


class HygienePack:
    """Mechanical checks. No model calls, so every tier supports it."""

    name = "hygiene"
    max_autonomy = 3

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
            yield from detector.detect(ctx)
