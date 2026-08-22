"""The hunt stage: read the code, propose candidates, and judge the run.

WHAT THIS LAYER IS FOR. `provider/claude_cli.py` deliberately decides nothing --
invariant 2 says the deterministic layer holds authority -- so it records
`denials`, `mutation` and `turns` and hands them on without an opinion. This is
the layer with the opinion. A stage that mutated the worktree or was refused a
tool produces no candidates here, whatever its payload looked like.

ONE STAGE PER ANGLE, SEQUENTIALLY. The plan said "in parallel", and that was
written before the budget was measured. `--max-budget-usd` below about $0.35
makes a stage a guaranteed no-op, so the ceiling had to become run-level -- and
a run-level ceiling can only stop between stages. Firing N angles at once
commits N stages' cost before the budget can react, which is the opposite of
what a ceiling is for. Sequential is what makes the ceiling mean something.

FINDING NOTHING IS AN ANSWER. An empty `findings` list is never retried. The
schema requires a non-empty `notes` exactly when `findings` is empty, so the
reason always exists and travels back in `HuntResult.notes` -- not as a skip,
because a skip means the check did not run and this one did.
"""

from __future__ import annotations

from pathlib import PurePosixPath
from string import Template
from typing import Any, NamedTuple

from ...policy.profiles import profile_for
from ...provider.base import Provider, StageRequest
from ...schemas import load_schema
from ..base import RunContext
from .prompts import load_prompt

# Angles a hunt looks along. Each is one stage: a model told to look for
# everything looks carefully at nothing, and a single finding cap across all
# concerns makes the model choose between unrelated things.
_DEFAULT_ANGLES: tuple[str, ...] = (
    "error handling: unguarded indexing, unchecked returns, swallowed exceptions",
    "boundaries: off-by-one, empty and single-element inputs, unbounded growth",
    "state and lifetime: resources not released, mutation of shared objects",
)

_DEFAULT_MAX_FINDINGS = 2

# How many files to name in the prompt. The list orients the model; it is not
# the access control, which is `--tools` and `--add-dir`.
_MAX_LISTED_FILES = 200


class HuntResult(NamedTuple):
    """Candidates, work not done, and answers that were not findings.

    THREE FIELDS, not the plan's two. `(candidates, skips)` had nowhere to put
    the reason an empty hunt gives, and a skip is the wrong home for it: a skip
    means the check did not run. Dropping it instead would put the tool back
    where an empty result is indistinguishable from a declined one, which is
    the exact hole the schema's conditional `notes` was added to close.

    A NamedTuple for the named access, not for compatibility: it unpacks as
    THREE values, so `candidates, skips = hunt(...)` raises `ValueError`. That
    is the right failure -- loud, at the call site, naming the arity -- rather
    than a pair that silently drops the notes.
    """

    candidates: tuple[dict[str, Any], ...]
    skips: tuple[str, ...]
    notes: tuple[str, ...]


def _angles(ctx: RunContext) -> tuple[str, ...]:
    configured = ctx.options.get("angles")
    if isinstance(configured, (list, tuple)) and configured:
        return tuple(str(angle) for angle in configured)
    return _DEFAULT_ANGLES


def _max_findings(ctx: RunContext) -> int:
    value = ctx.options.get("max_findings_per_angle", _DEFAULT_MAX_FINDINGS)
    return value if isinstance(value, int) and value > 0 else _DEFAULT_MAX_FINDINGS


def _split_subject(subject: str) -> tuple[str, tuple[int, int] | None]:
    """`app.py:12` -> `("app.py", (12, 12))`; `app.py:14-17` -> `("app.py", (14, 17))`.

    Only a trailing all-digit segment, or two of them joined by a hyphen, is
    treated as an address. A Windows path carries a colon after the drive
    letter, and a path may legitimately contain one elsewhere, so splitting on
    the first colon would mangle both.

    A RANGE USED TO BECOME PART OF THE PATH. `app.py:14-17` failed the digit
    test, so the entire string was taken as the filename, matched nothing in
    scope, and the finding was discarded as unplaceable -- while the message
    told the user their `boundaries.include` was wrong. Nothing in the hunt
    prompt asks for a single line, so whether a real finding survived depended
    on which format the model happened to choose. That is finding loss wearing
    the appearance of a clean run.

    A single line comes back as a one-line span rather than an int, so callers
    validate one shape instead of two. THE SUBJECT ITSELF IS NOT REWRITTEN:
    `dedupe_key` hashes it verbatim, and normalising `:14-17` to `:14` here
    would re-point every stored rejection at a key it was never filed under.
    Placing a finding and keying it are different jobs; this one only places it.
    """
    head, separator, tail = subject.rpartition(":")
    if not separator:
        return subject, None
    if tail.isdigit():
        return head, (int(tail), int(tail))
    start, hyphen, end = tail.partition("-")
    if hyphen and start.isdigit() and end.isdigit():
        return head, (int(start), int(end))
    return subject, None


def _subject_problem(ctx: RunContext, subject: object) -> str | None:
    """Why *subject* cannot be believed, or None.

    A CLAIM WITH A PHYSICAL REFERENT, so it is recomputed from the world rather
    than taken from the payload. The model controls this field completely; the
    prompt telling it to use a path from the list is an instruction, not a
    control. A fabricated or stale path otherwise reaches the user as a
    finding's address -- and `read_nothing` shows that a stage which called no
    tool at all can still produce one.
    """
    if not isinstance(subject, str) or not subject.strip():
        return f"subject {subject!r} is not a path"

    path_text, span = _split_subject(subject.strip())
    candidate = PurePosixPath(path_text.replace("\\", "/"))
    if candidate.is_absolute() or ".." in candidate.parts:
        return f"subject {subject!r} is not a path inside the project"

    in_scope = {file.as_posix() for file in ctx.files}
    if candidate.as_posix() not in in_scope:
        return (
            f"subject {subject!r} names a file that was not in scope for this "
            f"run, so the finding cannot be placed"
        )

    target = ctx.project_root / candidate
    try:
        text = target.read_text(encoding="utf-8", errors="surrogateescape")
    except OSError as exc:
        return f"subject {subject!r} could not be read: {type(exc).__name__}"

    if span is not None:
        start, end = span
        # splitlines(), not count("\n") + 1: a file ending in a newline has one
        # fewer line than that arithmetic claims, so the last real line of every
        # normally-terminated file would be off by one in the permissive
        # direction -- the check would accept an address one past the end.
        total = len(text.splitlines())
        if end < start:
            return (
                f"subject {subject!r} ends before it starts, so it names no "
                f"region of the file"
            )
        if start < 1 or end > total:
            # BOTH ENDS. A range that begins inside the file and runs off the
            # end is still an address the file does not have, and the message
            # repeats the span it was handed rather than a normalised one.
            where = f"line {start}" if start == end else f"lines {start}-{end}"
            return (
                f"subject {subject!r} points at {where} of a {total}-line "
                f"file, so the finding does not have the address it claims"
            )
    return None


def _prompt_for(ctx: RunContext, angle: str) -> str:
    listed = [path.as_posix() for path in ctx.files[:_MAX_LISTED_FILES]]
    if len(ctx.files) > _MAX_LISTED_FILES:
        listed.append(f"... and {len(ctx.files) - _MAX_LISTED_FILES} more")
    return Template(load_prompt("hunt")).safe_substitute(
        angle=angle,
        files="\n".join(f"- {path}" for path in listed) or "- (no files in scope)",
        max_findings=_max_findings(ctx),
    )


def hunt(ctx: RunContext, provider: Provider) -> HuntResult:
    """Run one stage per angle and return what survived judgement."""
    schema = load_schema("hunt")
    permissions = profile_for("hunt")

    candidates: list[dict[str, Any]] = []
    skips: list[str] = []
    notes: list[str] = []

    for angle in _angles(ctx):
        request = StageRequest(
            stage="hunt",
            prompt=_prompt_for(ctx, angle),
            schema=schema,
            permissions=permissions,
            effort=str(ctx.options.get("effort", "medium")),
            # None on purpose. A per-stage ceiling low enough to be useful is
            # low enough to make the stage a no-op -- measured. Task 9's budget
            # is run-level and stops between angles.
            max_budget_usd=None,
            cwd=ctx.project_root,
        )
        result = provider.run_stage(request)

        # The mutation check comes first and is unconditional. A read-only
        # stage that wrote is not a stage whose findings mean anything, and its
        # payload can look perfectly well-formed while it happens.
        if result.mutation:
            skips.append(
                f"hunt [{angle}] modified the worktree and its findings were "
                f"discarded: {result.mutation}"
            )
            continue
        if result.denials:
            skips.append(
                f"hunt [{angle}] was refused {', '.join(sorted(set(result.denials)))} "
                f"and answered on less than it asked for, so its findings were "
                f"discarded."
            )
            continue
        # Two conditions, two reasons. `StageResult` currently guarantees a
        # failure carries an error and a success carries data, so neither
        # fallback should fire -- and reading `result.error` into the message
        # regardless would print "did not run: None" the day one of those
        # guarantees moves. A reason that reaches the user has to be a reason.
        if not result.ok:
            skips.append(
                f"hunt [{angle}] did not run: "
                f"{result.error or 'the provider failed without saying why'}"
            )
            continue
        if result.data is None:
            skips.append(
                f"hunt [{angle}] returned success with no payload, so there is "
                f"nothing to read."
            )
            continue

        findings = result.data.get("findings") or []
        note = (result.data.get("notes") or "").strip()
        if note:
            notes.append(f"hunt [{angle}]: {note}")

        # NOT retried, and not a skip. See the module docstring.
        for finding in findings:
            problem = _subject_problem(ctx, finding.get("subject"))
            if problem is not None:
                skips.append(f"hunt [{angle}] discarded a finding: {problem}")
                continue
            candidates.append(
                {
                    **finding,
                    "provenance": {
                        "angle": angle,
                        "turns": result.turns,
                        # One turn means no tool was called, so nothing was
                        # read -- which is what a fabricated finding looks
                        # like. RECORDED, NOT DISCARDED: whether it correlates
                        # with junk is what Task 10 measures, and dropping
                        # these now would destroy the evidence needed to decide.
                        "read_nothing": result.turns <= 1,
                        "cost_usd": result.usage.cost_usd,
                        # total_tokens, never input_tokens: measured 4 against
                        # 41,036 on one call.
                        "tokens": result.usage.total_tokens,
                    },
                }
            )

    return HuntResult(tuple(candidates), tuple(skips), tuple(notes))
