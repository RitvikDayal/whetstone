"""The rendered-ui lens pack: drive, capture, verify-geometry, second pass.

THE SECOND LENS, AND THE POINT OF M2. The design says: "If two lens packs with
genuinely different evidence types do not fit the spine cleanly, the abstraction
is wrong." This pack implements `LensPack` -- `name`, `max_autonomy`,
`supports_tier`, `run` -- and that protocol is unchanged by this milestone. What
it produces is `EvidenceKind.capture`, which M0 wrote into the contract as "a
screenshot plus replayable navigation" before any browser code existed.

MAX AUTONOMY 1, AND THAT IS NOT TIMIDITY. `code-defects` earns 3 because a code
defect has an executable proof: a test that fails before a change and passes
after. A geometry finding has a MEASUREMENT, which is weaker -- it says two boxes
intersect at one viewport, not that any particular edit is the right fix. There
is no artifact this lens can hand an implementer that says "this is fixed", so it
does not get to open pull requests. It reports.

WHAT IS NOT HERE. No falsify stage. The design's graph for this lens is drive ->
capture -> verify-geometry -> falsify, and the first three are what M2 built. A
falsifier over a controller-measured intersection would be arguing with
arithmetic: the second pass is what challenges a geometry claim, because the
thing that can be wrong about it is that it did not reproduce. This is a
DEPARTURE FROM THE PLAN and it is recorded as one rather than quietly dropped --
see the milestone's verdict.
"""

from __future__ import annotations

import math
from collections.abc import Iterator
from typing import TYPE_CHECKING

from ...budget import Budget, BudgetedProvider
from ...errors import WhetstoneError
from ...severity import Severity
from ..base import (
    Candidate,
    Evidence,
    EvidenceKind,
    LensRuntime,
    LensScope,
    RunContext,
)
from .browser import Box, BrowserError, Origin, availability
from .capture import (
    DEFAULT_MIN_OVERLAP_PX,
    DEFAULT_STABILITY_TOLERANCE,
    Overlap,
    capture,
)
from .drive import drive

if TYPE_CHECKING:  # pragma: no cover - import shape only
    from ...provider.base import Provider

_NO_MODEL_TIER = "quick"
_RULE_ID = "overlap"
_DEFAULT_VIEWPORTS: tuple[tuple[int, int], ...] = ((1280, 800),)


def _is_size(value: object) -> bool:
    """A positive int that is not a bool.

    `isinstance(True, int)` is true, so `viewports: [[true, true]]` measured a
    1x1 page without a word about it. Booleans are excluded explicitly because
    Python will not do it for us.
    """
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def _viewports(ctx: RunContext) -> tuple[tuple[int, int], ...]:
    """Declared viewports, or one desktop width -- SAYING SO when it substitutes.

    ANY lens that needs the app running needs these, which is the argument the
    abstraction gate accepted for `viewport` appearing in config: a product-ux
    lens measuring reading width wants the same field. It is not browser
    vocabulary leaking into the spine.

    Every rejection is reported. A silently substituted default means the lens
    measured under settings the user did not declare, and the report then reads
    as a result about the declared configuration -- which is the same class of
    lie as a run that quietly checked half the surface.
    """
    raw = ctx.options.get("viewports")
    if raw is None:
        return _DEFAULT_VIEWPORTS
    if not isinstance(raw, (list, tuple)) or not raw:
        ctx.skip(
            f"rendered-ui: `options.viewports` is {raw!r}, which is not a "
            f"non-empty list, so {_DEFAULT_VIEWPORTS[0][0]}x"
            f"{_DEFAULT_VIEWPORTS[0][1]} was measured instead of what you "
            f"declared."
        )
        return _DEFAULT_VIEWPORTS

    out: list[tuple[int, int]] = []
    for item in raw:
        if (
            isinstance(item, (list, tuple))
            and len(item) == 2
            and all(_is_size(n) for n in item)
        ):
            out.append((int(item[0]), int(item[1])))
        else:
            ctx.skip(
                f"rendered-ui: the viewport {item!r} is not a pair of positive "
                f"whole numbers and was not measured."
            )
    if not out:
        ctx.skip(
            f"rendered-ui: no declared viewport was usable, so "
            f"{_DEFAULT_VIEWPORTS[0][0]}x{_DEFAULT_VIEWPORTS[0][1]} was "
            f"measured instead. A geometry finding is only true at a width, so "
            f"this is not the run you asked for."
        )
        return _DEFAULT_VIEWPORTS
    return tuple(out)


def _float_option(ctx: RunContext, key: str, fallback: float) -> float:
    """A declared threshold, or the default -- and it says when it substitutes."""
    if key not in ctx.options:
        return fallback
    value = ctx.options[key]
    # `math.isfinite` FIRST, and it is not tidiness. Every comparison against
    # NaN is False, so `min_overlap_px: nan` makes `smaller < min_overlap_px`
    # false for every measurement and a ZERO-AREA result is reported as an
    # overlap -- the lens inventing findings rather than missing them. `inf`
    # fails the other way and silently suppresses all of them. Both satisfy
    # `isinstance(value, float)` and `value >= 0`.
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        ctx.skip(
            f"rendered-ui: `options.{key}` is {value!r}, which is not a "
            f"non-negative number, so {fallback} was used instead."
        )
        return fallback
    return float(value)


class RenderedUiPack:
    """Four stages over one origin, under the run's ceiling."""

    name = "rendered-ui"
    # See the module docstring. A measurement is not an executable proof.
    max_autonomy = 1
    # FILE-SCOPED, and the first real run is what settled it. `project` looked
    # right -- this lens measures a running app, not files -- but the runner
    # does not resolve a file list at all when no enabled lens is file-scoped,
    # so the drive stage was handed "(no files in scope)" and asked to read
    # markup it could not see. The stage's INPUT is the app's own source, and
    # `boundaries.include` narrowing which templates get read is correct rather
    # than misleading.
    scope = LensScope.file

    def __init__(
        self,
        *,
        provider: Provider | None = None,
        provider_name: str | None = None,
        ceiling_usd: float | None = None,
        calls_per_day: int | None = None,
    ) -> None:
        self._provider = provider
        self.provider_name = provider_name
        self.ceiling_usd = ceiling_usd
        self.calls_per_day = calls_per_day

    def configure(self, runtime: LensRuntime) -> RenderedUiPack:
        """A copy carrying what this run's config decided.

        `test_command` is deliberately not read: this lens never runs the
        target's tests. Taking a field a pack does not use is how a contract
        widens by accident.
        """
        return type(self)(
            provider=self._provider,
            provider_name=runtime.provider_name,
            ceiling_usd=runtime.ceiling_usd,
            calls_per_day=runtime.calls_per_day,
        )

    def supports_tier(self, tier: str) -> bool:
        """`quick` does not run this lens: the drive stage is a model call."""
        return tier != _NO_MODEL_TIER

    def run(self, ctx: RunContext) -> Iterator[Candidate]:
        return iter(self._collect(ctx))

    def _collect(self, ctx: RunContext) -> list[Candidate]:
        if not self.supports_tier(ctx.tier):
            ctx.skip(
                f"rendered-ui: not run at tier '{ctx.tier}'. The drive stage is a "
                f"model call and costs real money, so it is off at "
                f"'{_NO_MODEL_TIER}' by design."
            )
            return []

        base_url = ctx.options.get("base_url")
        if not isinstance(base_url, str) or not base_url.strip():
            ctx.skip(
                "rendered-ui: no `base_url` is declared in this lens's options, "
                "so there is no running app to look at and nothing was rendered. "
                "Set lenses.rendered-ui.options.base_url to the local origin the "
                "app serves on."
            )
            return []

        try:
            origin = Origin.parse(base_url.strip())
        except BrowserError as exc:
            ctx.skip(f"rendered-ui: {exc} Nothing was rendered.")
            return []

        blocked = availability()
        if blocked is not None:
            ctx.skip(f"rendered-ui: {blocked}")
            return []

        try:
            provider = self._resolve_provider()
        except WhetstoneError as exc:
            ctx.skip(f"rendered-ui: {exc} No stage of this lens ran.")
            return []

        budget = Budget(ceiling_usd=self.ceiling_usd)
        budgeted = BudgetedProvider(provider, budget, subject="(drive)")
        viewports = _viewports(ctx)

        proposal = drive(ctx, budgeted, origin, viewports)
        for reason in proposal.skips:
            ctx.skip(f"rendered-ui: {reason}")
        for note in proposal.notes:
            ctx.skip(f"rendered-ui: drive reported (not a skip) -- {note}")

        if not proposal.checks:
            return []

        stop = budget.reason()
        if stop is not None:
            ctx.skip(
                f"rendered-ui: the run-level ceiling stopped this lens before "
                f"anything was rendered ({stop}). "
                f"{len(proposal.checks)} proposed checks were not measured."
            )
            return []

        shots = ctx.state_root / "shots" / ctx.run_id
        shots.mkdir(parents=True, exist_ok=True)

        measured = capture(
            origin,
            proposal.checks,
            viewports,
            shots,
            min_overlap_px=_float_option(ctx, "min_overlap_px", DEFAULT_MIN_OVERLAP_PX),
            tolerance=_float_option(
                ctx, "stability_tolerance", DEFAULT_STABILITY_TOLERANCE
            ),
        )
        for reason in measured.skips:
            ctx.skip(reason)

        return [self._candidate(o, origin) for o in measured.overlaps]

    def _candidate(self, overlap: Overlap, origin: Origin) -> Candidate:
        width, height = overlap.viewport
        check = overlap.check
        area = overlap.overlap_px
        return Candidate(
            lens=self.name,
            rule_id=_RULE_ID,
            # The address of a rendered defect is route plus viewport. The same
            # two elements at 360px and at 1280px are different findings, and a
            # subject that dropped the width would dedupe them into one.
            subject=f"{check.route}@{width}x{height}",
            title=(
                f"{check.selector_a} and {check.selector_b} overlap by "
                f"{area:.0f} square pixels"
            ),
            detail=(
                f"Measured twice in separate browser contexts at {width}x{height}. "
                f"First pass {overlap.first.overlap_px:.1f} square pixels, second "
                f"{overlap.second.overlap_px:.1f}; the smaller is reported. "
                f"The drive stage proposed this pair because: {check.why}"
            ),
            # Not derived from the area. A big overlap of two decorative
            # elements is not more severe than a small one over a submit
            # button, and this lens cannot tell those apart -- so it does not
            # pretend to. A human triages it.
            severity=Severity.medium,
            evidence=Evidence(
                kind=EvidenceKind.capture,
                summary=(
                    f"{area:.1f} square pixels of overlap at {width}x{height}, "
                    f"measured by the controller in two separate renders"
                ),
                data={
                    "route": check.route,
                    "viewport": [width, height],
                    "selector_a": check.selector_a,
                    "selector_b": check.selector_b,
                    "first_pass_px": overlap.first.overlap_px,
                    "second_pass_px": overlap.second.overlap_px,
                    "box_a": _box_json(overlap.first.box_a),
                    "box_b": _box_json(overlap.first.box_b),
                    # The replayable navigation. Invariant 5: a fix is checked
                    # by rendering the same route at the same viewport and
                    # measuring the same two selectors again.
                    #
                    # THE FULL URL, plus its parts. `url` held a bare path and
                    # nothing recorded the origin, so a consumer following the
                    # replay instruction had no host or port to navigate to --
                    # the evidence was not replayable, which is the one thing
                    # the key name promised.
                    "replay": {
                        "url": f"{origin}{check.route}",
                        "origin": str(origin),
                        "route": check.route,
                        "viewport": [width, height],
                        "measure": [check.selector_a, check.selector_b],
                    },
                },
                artifacts=(
                    (str(overlap.screenshot),) if overlap.screenshot else ()
                ),
            ),
        )

    def _resolve_provider(self) -> Provider:
        if self._provider is not None:
            return self._provider
        # Imported here rather than at module scope, for the reason
        # `code_defects/pack.py` gives: the provider registry instantiates the
        # CLI provider at import time, and the lens registry imports this
        # module during `whetstone --help`.
        from ...provider.registry import get_provider

        # The same default `code-defects` takes. Raising instead was measured
        # wrong on the first real run: a config with no `model.provider` key is
        # the ordinary case, and the lens skipped itself with "no provider is
        # configured" while the other pack ran perfectly on the same file.
        #
        # `is None`, not `or`. `ModelConfig.provider` neither rejects nor
        # normalises an empty string, so `provider: ""` in a config file is a
        # typo the `or` spelling silently turns into "the default is fine".
        # An explicit empty name reaches `get_provider` and is refused by name.
        if self.provider_name is None:
            return get_provider("claude-cli")
        return get_provider(self.provider_name)


def _box_json(box: Box | None) -> dict[str, float] | None:
    if box is None:
        return None
    return {"x": box.x, "y": box.y, "width": box.width, "height": box.height}
