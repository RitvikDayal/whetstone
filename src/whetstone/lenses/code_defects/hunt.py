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

    A NamedTuple so it still unpacks positionally for anyone expecting a pair
    to have grown, rather than failing at a call site far from here.
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
        if not result.ok or result.data is None:
            skips.append(f"hunt [{angle}] did not run: {result.error}")
            continue

        findings = result.data.get("findings") or []
        note = (result.data.get("notes") or "").strip()
        if note:
            notes.append(f"hunt [{angle}]: {note}")

        # NOT retried, and not a skip. See the module docstring.
        for finding in findings:
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
