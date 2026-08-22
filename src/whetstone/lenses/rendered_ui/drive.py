"""The drive stage: ask where to look, never what is true.

WHAT THIS STAGE IS AND IS NOT. It reads the app's own markup and proposes PAIRS
OF SELECTORS worth measuring. It does not decide that anything overlaps. The
controller renders the page, measures both boxes and computes the intersection
itself, and the model's opinion about the result is never consulted -- the same
treatment `grade.py` gives `model_confidence`, for the same reason. A claim about
pixels that was never measured in pixels is not evidence.

EVERY ROUTE IS CHECKED AGAINST THE ORIGIN HERE, before anything is navigated.
`Page.goto` refuses a foreign origin too, but a route that would be refused is a
proposal this stage should never have carried forward, and discovering that at
navigation time turns a bad proposal into a failed render. Checked twice on
purpose: this one produces a reason, the adapter's is the guard.

A SELECTOR THAT MATCHES NOTHING IS NOT AN ERROR AND NOT A FINDING. It is
recorded as its own outcome. An element that is absent and an element that
collapsed to zero size are different facts about a page, and reporting the first
as the second is how a lens invents a defect.
"""

from __future__ import annotations

from string import Template
from typing import NamedTuple

from ...policy.profiles import READ_ONLY
from ...provider.base import Provider, StageRequest
from ..base import RunContext
from .browser import Origin
from .prompts import load_prompt
from .schemas import load_schema

# How many pairs one stage may propose. A model told to cover the page proposes
# a list nobody can afford to render; the cap is what keeps the measurement
# affordable, and it is the same argument `max_findings_per_angle` makes.
_DEFAULT_MAX_CHECKS = 6

# How many files to name in the prompt. Orientation, not access control --
# `--tools` and `--add-dir` are that.
_MAX_LISTED_FILES = 200


class Check(NamedTuple):
    """One pair of selectors to measure, at one route."""

    route: str
    selector_a: str
    selector_b: str
    why: str


class DriveResult(NamedTuple):
    """Checks, work not done, and answers that were not checks.

    Three fields for the reason `HuntResult` gives: an empty proposal is a real
    answer and a skip is the wrong home for it, because a skip means the stage
    did not run and this one did.
    """

    checks: tuple[Check, ...]
    skips: tuple[str, ...]
    notes: tuple[str, ...]


def _schema_ceiling() -> int | None:
    """The most checks the drive contract permits in one answer, or None.

    READ FROM THE SCHEMA, not restated here. Two numbers that must agree and
    live in different files do not stay agreed, and the failure is silent: the
    prompt asks for a cap the model is structurally unable to deliver. None
    means the contract sets no ceiling, which is a reason not to clamp rather
    than a reason to fall back to the default.
    """
    ceiling = load_schema("drive").get("properties", {}).get("checks", {}).get("maxItems")
    return ceiling if isinstance(ceiling, int) and ceiling > 0 else None


def _max_checks(ctx: RunContext) -> tuple[int, str | None]:
    """The cap to enforce, and why a configured value was refused.

    `bool` is excluded explicitly: it is an `int` subclass, so `max_checks: true`
    passed every test here and became a cap of ONE rather than the default.
    Python will not do this for us and it has now bitten this lens in three
    separate options.

    THE REASON IS RETURNED RATHER THAN SWALLOWED. Falling back is declining to
    do work the caller asked for -- `max_checks: "20"` means somebody wanted 20
    and got 6 -- and a run that silently measured less than the configured
    surface reads as clean. Absent is not refused: the default arrives here as a
    valid value and produces no reason, so only an explicit bad one is reported.
    """
    value = ctx.options.get("max_checks", _DEFAULT_MAX_CHECKS)
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return _DEFAULT_MAX_CHECKS, (
            f"`options.max_checks` is {value!r}, which is not a positive whole "
            f"number. The cap fell back to {_DEFAULT_MAX_CHECKS}, so this run "
            f"measured less than you configured."
        )
    ceiling = _schema_ceiling()
    if ceiling is not None and value > ceiling:
        # ABOVE THE CONTRACT IS ALSO DECLINING TO DO WORK. `max_checks: 20`
        # put 20 in the prompt while the schema refuses an answer longer than
        # 12, so the run measured less than it was configured for and the
        # truncation branch never fired to say so -- the cap was never reached
        # because it could not be.
        return ceiling, (
            f"`options.max_checks` is {value}, above the {ceiling} the drive "
            f"contract allows in one answer. The cap is {ceiling}; a larger "
            f"one could not have been filled."
        )
    return value, None


def _prompt_for(
    ctx: RunContext,
    origin: Origin,
    viewports: tuple[tuple[int, int], ...],
    max_checks: int,
) -> str:
    listed = [path.as_posix() for path in ctx.files[:_MAX_LISTED_FILES]]
    if len(ctx.files) > _MAX_LISTED_FILES:
        listed.append(f"... and {len(ctx.files) - _MAX_LISTED_FILES} more")
    return Template(load_prompt("drive")).safe_substitute(
        origin=str(origin),
        files="\n".join(f"- {path}" for path in listed) or "- (no files in scope)",
        viewports="\n".join(f"- {w}x{h}" for w, h in viewports),
        max_checks=max_checks,
    )


def _check_problem(origin: Origin, raw: object) -> str | None:
    """Why *raw* cannot be measured, or None.

    A claim with a physical referent, recomputed rather than believed. The schema
    guarantees the shape; it cannot guarantee that the route names a page on the
    app under test, and that is the part with consequences.
    """
    if not isinstance(raw, dict):
        return f"check {raw!r} is not an object"
    route = raw.get("route")
    if not isinstance(route, str) or not route.startswith("/"):
        return (
            f"route {route!r} is not a path beginning with '/', so it names no "
            f"page on {origin}"
        )
    if route.startswith("//"):
        # A PROTOCOL-RELATIVE URL, not a path. `//evil.test/` passes the check
        # above, and concatenating it onto the origin happens to neutralise it
        # HERE -- but it is stored verbatim as the replay URL, and anything that
        # navigates to that value directly resolves it to `http://evil.test/`.
        # Refused rather than sanitised: the model had no business proposing it.
        return (
            f"route {route!r} is a protocol-relative URL rather than a path. It "
            f"names a host, not a page on {origin}."
        )
    if not origin.admits(f"{origin}{route}"):
        return (
            f"route {route!r} does not resolve inside {origin}. This lens is "
            f"pinned to one origin by scheme, host and port."
        )
    for key in ("selector_a", "selector_b"):
        value = raw.get(key)
        if not isinstance(value, str) or not value.strip():
            return f"{key} {value!r} is not a selector"
    if raw["selector_a"].strip() == raw["selector_b"].strip():
        return (
            f"selector_a and selector_b are both {raw['selector_a']!r}. An "
            f"element always intersects itself, so this check can only produce "
            f"a false finding."
        )
    return None


def drive(
    ctx: RunContext,
    provider: Provider,
    origin: Origin,
    viewports: tuple[tuple[int, int], ...],
) -> DriveResult:
    """Ask one stage where to look, and keep only what can be measured."""
    # BOTH LISTS EXIST BEFORE THE REQUEST. The cap is decided here, once, and
    # its refusal reason has to outlive the early returns below -- a stage that
    # rejected your `max_checks` and then died on a denial must still say it
    # rejected your `max_checks`.
    skips: list[str] = []
    notes: list[str] = []
    cap, cap_refused = _max_checks(ctx)
    if cap_refused is not None:
        skips.append(cap_refused)

    request = StageRequest(
        stage="drive",
        prompt=_prompt_for(ctx, origin, viewports, cap),
        schema=load_schema("drive"),
        # The lens's own read-only powers, reusing the spine's audited set
        # rather than registering a stage name in `PROFILES`. This stage reads
        # markup and proposes selectors; it never renders anything, and the
        # browser is the controller's.
        permissions=READ_ONLY,
        effort=str(ctx.options.get("effort", "medium")),
        # None, deferring to the run-level ceiling. A per-stage bound low enough
        # to be useful is low enough to make the stage a no-op -- measured in
        # M1a, and nothing about this stage changes that.
        max_budget_usd=None,
        cwd=ctx.project_root,
    )
    result = provider.run_stage(request)

    # Unconditional and first. A read-only stage that wrote is not a stage whose
    # proposals mean anything, and its payload can look perfectly well-formed
    # while it happens.
    if result.mutation:
        skips.append(
            f"drive modified the worktree and its proposals were discarded: "
            f"{result.mutation}"
        )
        return DriveResult((), tuple(skips), tuple(notes))
    if result.denials:
        skips.append(
            f"drive was refused {', '.join(sorted(set(result.denials)))} and "
            f"answered on less than it asked for, so its proposals were "
            f"discarded."
        )
        return DriveResult((), tuple(skips), tuple(notes))
    if not result.ok:
        skips.append(
            f"drive did not run: "
            f"{result.error or 'the provider failed without saying why'}"
        )
        return DriveResult((), tuple(skips), tuple(notes))
    if result.data is None:
        skips.append(
            "drive returned success with no payload, so there is nothing to read."
        )
        return DriveResult((), tuple(skips), tuple(notes))

    # TYPE-GUARDED, though the schema already forbids anything else. This layer
    # exists to recompute what a model claims rather than believe it, and
    # `"notes": 3` reaching `.strip()` ends the lens run with an unhandled
    # AttributeError instead of a recorded reason -- the schema is the model's
    # side of the claim, and a model's self-assessment is never trusted.
    raw_note = result.data.get("notes")
    if isinstance(raw_note, str) and raw_note.strip():
        notes.append(f"drive: {raw_note.strip()}")
    elif raw_note is not None and not isinstance(raw_note, str):
        skips.append(
            f"drive returned {type(raw_note).__name__} for `notes` rather than "
            f"text, so whatever it wanted to say about its own run was lost."
        )

    raw_checks = result.data.get("checks")
    if raw_checks is None:
        raw_checks = []
    elif not isinstance(raw_checks, list):
        # `skips`, not a fresh tuple. Building a new one here discarded the
        # reason recorded two lines above when BOTH fields were malformed -- a
        # path that declines to do work and then drops its own explanation on
        # the way out.
        skips.append(
            f"drive returned {type(raw_checks).__name__} for `checks` rather "
            f"than a list, so nothing could be measured."
        )
        return DriveResult((), tuple(skips), tuple(notes))

    # THE CAP IS ENFORCED HERE, not only asked for in the prompt. The schema
    # permits 12 and the configured cap may be lower, so a bound the caller
    # believes it set was one nothing enforced -- the exact defect
    # `StageRequest` names about `max_budget_usd`. The surplus costs two real
    # renders per viewport each, so it is not free to wave through.
    if len(raw_checks) > cap:
        skips.append(
            f"drive proposed {len(raw_checks)} checks and the cap is {cap}; the "
            f"last {len(raw_checks) - cap} were not measured. Raise "
            f"`options.max_checks` if they mattered."
        )
        raw_checks = raw_checks[:cap]

    checks: list[Check] = []
    for raw in raw_checks:
        problem = _check_problem(origin, raw)
        if problem is not None:
            skips.append(f"drive discarded a check: {problem}")
            continue
        checks.append(
            Check(
                route=raw["route"],
                selector_a=raw["selector_a"].strip(),
                selector_b=raw["selector_b"].strip(),
                why=str(raw.get("why") or "").strip(),
            )
        )
    return DriveResult(tuple(checks), tuple(skips), tuple(notes))
