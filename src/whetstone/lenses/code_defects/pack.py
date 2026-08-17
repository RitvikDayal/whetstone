"""The code-defects lens pack: four stages, one pipeline, one ceiling.

WHAT THIS FILE ADDS THAT THE STAGES DO NOT HAVE. `hunt`, `reproduce`, `falsify`
and `grade_finding` each work and each is tested, and none of them can be
reached from `whetstone run` because nothing decides the order, supplies the
config, or stops the spending. That is this file.

THE BUDGET IS RUN-LEVEL AND ENFORCED AT THE PROVIDER. Measured: the CLI's own
`--max-budget-usd` below about $0.35 makes a stage a guaranteed no-op, so a
per-stage ceiling small enough to bound anything is small enough to break what
it bounds. Every `StageRequest` therefore carries `max_budget_usd=None` and the
ceiling lives in `budget.py`. It is enforced by wrapping the provider rather
than by checking between candidates, because `hunt` runs one stage per angle
inside a single call -- a check between candidates could not stop it, and a
run-level ceiling that only bites after the most expensive stage is decoration.

STOPPING IS NOT SILENCE. Hitting the ceiling records every candidate it did not
reach, by name, with what was left. A run that quietly examined half the surface
reads as clean, which is the failure this project exists to prevent.

`challenged`, NOT `confirmed`, DECIDES WHETHER A FINDING IS GRADED AT ALL.
Task 8 added that field because `confirmed: false` cannot distinguish a finding
the falsifier killed from one it never reached -- a stage that mutated the
worktree, was refused a tool, or never ran leaves `confirmed` False for reasons
that have nothing to do with the finding. Grading those D would report a real
defect as dismissed by a stage that never looked at it, so an unchallenged
candidate is not recorded and its subject is named in a skip instead.

WHAT COST IS RECORDED, AND WHY IT IS NOT ESTIMATED. Every stage's `Usage` goes
into a per-run file under the state directory: stage, subject, cost, all four
token fields summed, wall time, and which envelope key the numbers came from.
The estimator is fit to this after Task 10. The predecessor's estimates were
4-17x low, and the reason was unrecoverable because only a total survived.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import TYPE_CHECKING, Any

from ...budget import Budget, BudgetedProvider
from ...errors import LensError, WhetstoneError
from ...severity import Severity
from ..base import (
    Candidate,
    Evidence,
    EvidenceKind,
    LensRuntime,
    LensScope,
    RunContext,
)
from .falsify import falsify
from .grade import Grade, grade_finding
from .hunt import hunt
from .reproduce import reproduce

if TYPE_CHECKING:  # pragma: no cover - import shape only
    from ...provider.base import Provider

# The tier that does not run this lens. Every other tier does, and the
# difference between them is which candidates are carried past the hunt.
_NO_MODEL_TIER = "quick"

# At `standard`, only these are carried through reproduce and falsify.
_STANDARD_SEVERITIES = frozenset({"high", "critical"})

# The model's severity vocabulary is the hunt schema's, which has an `info`
# level that `Severity` does not. Mapped to `low` rather than refused: `info` is
# a real answer the schema invites, and the model's own word is kept in the
# evidence so the mapping is visible rather than lossy. Anything NOT in this
# table discards the candidate with a reason -- issue #9 is what happens when a
# severity is allowed to become plausible-looking text instead.
_SEVERITY_FROM_MODEL: dict[str, Severity] = {
    "critical": Severity.critical,
    "high": Severity.high,
    "medium": Severity.medium,
    "low": Severity.low,
    "info": Severity.low,
}

# One rule per lens, so the same defect at the same address dedupes across runs.
# Deliberately not derived from the title: `Candidate.dedupe_key` excludes
# title precisely so a reworded finding cannot resurrect a rejection, and
# folding the wording back in through rule_id would undo that.
_RULE_ID = "defect"


class CodeDefectsPack:
    """Hunt, reproduce, falsify, grade -- in that order, under one ceiling.

    Constructed once by the registry and CONFIGURED PER RUN: `configure` returns
    a new instance rather than mutating this one, because the registry hands out
    a single object for the life of the process and one project's test command
    must not reach the next project's run. The `Budget` is built inside `run`
    for the same reason.
    """

    name = "code-defects"
    max_autonomy = 3
    # File-scoped: the hunt is told which files are in scope and every candidate
    # subject is checked against that list, so boundaries.include/exclude really
    # do narrow what this lens examines.
    scope = LensScope.file

    def __init__(
        self,
        *,
        provider: Provider | None = None,
        provider_name: str | None = None,
        test_command: str | None = None,
        ceiling_usd: float | None = None,
        calls_per_day: int | None = None,
    ) -> None:
        self._provider = provider
        self.provider_name = provider_name
        self.test_command = test_command
        self.ceiling_usd = ceiling_usd
        self.calls_per_day = calls_per_day

    def configure(self, runtime: LensRuntime) -> CodeDefectsPack:
        """A copy of this pack carrying what the run's config decided.

        Called by the runner, which is the only layer holding the config.
        `RunContext` deliberately does not carry it -- a lens reading
        `whetstone.yaml` for itself would duplicate the loader and walk around
        the boundary that keeps a third-party pack from reading a project's
        secrets. `LensRuntime` rather than `WhetstoneConfig` for the same
        reason: this pack needs four values, and the config object it used to
        receive carries a resolved `state_dir` secret.
        """
        return type(self)(
            provider=self._provider,
            provider_name=runtime.provider_name,
            test_command=runtime.test_command,
            ceiling_usd=runtime.ceiling_usd,
            calls_per_day=runtime.calls_per_day,
        )

    def supports_tier(self, tier: str) -> bool:
        """`quick` does not run this lens: every stage of it costs money."""
        return tier != _NO_MODEL_TIER

    def run(self, ctx: RunContext) -> Iterator[Candidate]:
        """Everything happens before the first candidate is yielded.

        NOT a generator. A generator would leave the cost record unwritten for
        any consumer that stopped iterating early, and the whole pipeline's
        spend would vanish with it. Eager means the ledger is written on every
        path out of `_collect`.
        """
        return iter(self._collect(ctx))

    # -- the pipeline ------------------------------------------------------------

    def _collect(self, ctx: RunContext) -> list[Candidate]:
        if not self.supports_tier(ctx.tier):
            ctx.skip(
                f"code-defects: not run at tier '{ctx.tier}'. Every stage of this "
                f"lens is a model call and costs real money, so it is off at "
                f"'{_NO_MODEL_TIER}' by design. Use 'standard' or 'deep'."
            )
            return []
        if not ctx.files:
            ctx.skip(
                "code-defects: no files were in scope, so there was nothing to "
                "hunt through and no model call was made."
            )
            return []

        try:
            provider = self._resolve_provider()
        except WhetstoneError as exc:
            ctx.skip(f"code-defects: {exc} No stage of this lens ran.")
            return []

        budget = Budget(ceiling_usd=self.ceiling_usd)
        self._report_budget_gaps(ctx)
        budgeted = BudgetedProvider(provider, budget, subject="(hunt)")

        try:
            return self._pipeline(ctx, budgeted, budget)
        finally:
            self._write_cost_record(ctx, budget)

    def _pipeline(
        self, ctx: RunContext, budgeted: BudgetedProvider, budget: Budget
    ) -> list[Candidate]:
        result = hunt(ctx, budgeted)
        for reason in result.skips:
            ctx.skip(f"code-defects: {reason}")
        for note in result.notes:
            # A note is NOT a skip -- the hunt ran and this is what it reported.
            # It goes through `ctx.skip` because that is the only channel a lens
            # has to the user, and losing the reason an empty hunt gives would
            # put an honest "I read it and found nothing" back where it is
            # indistinguishable from a stage that declined.
            ctx.skip(f"code-defects: hunt reported (not a skip) -- {note}")

        carried = self._carried(ctx, result.candidates)
        if self.test_command is None and carried:
            ctx.skip(
                "code-defects: `environment.commands.test` is not declared, so "
                "there is no command to execute a reproduction with. The "
                "reproduce stage was not run at all and no finding from this "
                "run can be graded above C."
            )

        found: list[Candidate] = []
        for index, candidate in enumerate(carried):
            stop = budget.reason()
            if stop is not None:
                ctx.skip(self._not_reached(carried[index:], budget, stop))
                break
            recorded = self._one(ctx, candidate, budgeted)
            if recorded is not None:
                found.append(recorded)
        return found

    def _one(
        self, ctx: RunContext, candidate: dict[str, Any], budgeted: BudgetedProvider
    ) -> Candidate | None:
        subject = str(candidate.get("subject"))
        budgeted.subject = subject

        reproduction, skips = self._reproduce(ctx, candidate, budgeted)
        for reason in skips:
            ctx.skip(f"code-defects [{subject}]: {reason}")

        verdict, skips = falsify(candidate, reproduction, ctx, budgeted)
        for reason in skips:
            ctx.skip(f"code-defects [{subject}]: {reason}")

        if not verdict["challenged"]:
            # See the module docstring. `confirmed` is False here because
            # nothing challenged the finding, not because something killed it,
            # and grade_finding cannot tell those apart from its arguments.
            ctx.skip(
                f"code-defects [{subject}]: nothing challenged this candidate, so "
                f"it was neither confirmed nor killed and is NOT recorded. It was "
                f"found, and it has not been judged -- re-run to have it "
                f"challenged."
            )
            return None

        grade, why = grade_finding(
            # From the CONTROLLER'S own exit code. The reproduce payload carries
            # the model's claim about itself and it is inside `payload`, where
            # nothing here reads it.
            reproduced=bool(reproduction["reproduced"]),
            has_runnable_artifact=bool(reproduction["has_runnable_artifact"]),
            falsifier_confirmed=bool(verdict["confirmed"]),
            alternative_explanations=len(
                candidate.get("alternative_explanations") or []
            ),
            # Recorded for calibration and discarded by `grade_finding` itself.
            model_confidence=candidate.get("confidence"),
        )
        return self._candidate(ctx, candidate, reproduction, verdict, grade, why)

    def _reproduce(
        self, ctx: RunContext, candidate: dict[str, Any], budgeted: BudgetedProvider
    ) -> tuple[dict[str, Any], list[str]]:
        if self.test_command is None:
            # Not a wasted model call. Without a test command the artifact could
            # never be executed, so asking for one buys nothing and costs money.
            # The reason was recorded once, in `_pipeline`.
            return (
                {
                    "reproduced": False,
                    "verdict": "not attempted",
                    "executed": False,
                    "has_runnable_artifact": False,
                    "mutation": None,
                    "payload": None,
                    "provenance": {},
                },
                [],
            )
        return reproduce(
            candidate,
            ctx,
            budgeted,
            self.test_command,
            self._sandbox_image(ctx),
        )

    # -- what gets carried, and what does not ------------------------------------

    def _carried(
        self, ctx: RunContext, candidates: tuple[dict[str, Any], ...]
    ) -> list[dict[str, Any]]:
        """Candidates this tier will carry through the remaining stages.

        A candidate dropped here is named in a skip. At `standard` the drop is
        the tier doing its job -- only high-severity findings are worth three
        more model calls -- and at any tier an unmappable severity is a payload
        this pipeline cannot record without inventing a value for it.
        """
        carried: list[dict[str, Any]] = []
        for candidate in candidates:
            declared = candidate.get("severity")
            severity = _SEVERITY_FROM_MODEL.get(str(declared))
            title = str(candidate.get("title"))
            subject = str(candidate.get("subject"))
            if severity is None:
                ctx.skip(
                    f"code-defects [{subject}]: discarded '{title}' -- its severity "
                    f"{declared!r} is not one of "
                    f"{', '.join(sorted(_SEVERITY_FROM_MODEL))}, and recording a "
                    f"finding under a severity nothing else uses hides it from "
                    f"every filter."
                )
                continue
            if ctx.tier == "standard" and str(declared) not in _STANDARD_SEVERITIES:
                ctx.skip(
                    f"code-defects [{subject}]: '{title}' was found at severity "
                    f"{declared} and NOT carried through reproduce or falsify at "
                    f"tier standard, which only does that for "
                    f"{'/'.join(sorted(_STANDARD_SEVERITIES))}. It is unjudged, "
                    f"not absent -- re-run at tier deep to have it checked."
                )
                continue
            carried.append(candidate)
        return carried

    def _not_reached(
        self, remaining: list[dict[str, Any]], budget: Budget, stop: str
    ) -> str:
        """Every candidate the ceiling stopped us reaching, by name."""
        left = budget.remaining()
        names = "; ".join(
            f"{candidate.get('subject')} ({candidate.get('title')})"
            for candidate in remaining
        )
        return (
            f"code-defects: {stop} {len(remaining)} candidate(s) were found and "
            f"NOT checked: {names}. Remaining budget: "
            f"{'none' if left is None else f'${left:.4f}'}. They are unjudged, "
            f"not absent -- raise `budget.ceiling.usd_per_run` and re-run."
        )

    # -- recording ---------------------------------------------------------------

    def _candidate(
        self,
        ctx: RunContext,
        candidate: dict[str, Any],
        reproduction: dict[str, Any],
        verdict: dict[str, Any],
        grade: Grade,
        why: str,
    ) -> Candidate | None:
        subject = str(candidate.get("subject"))
        declared = str(candidate.get("severity"))
        severity = _SEVERITY_FROM_MODEL[declared]
        adjusted = _SEVERITY_FROM_MODEL.get(str(verdict.get("severity_adjustment")))
        if adjusted is not None:
            severity = adjusted

        # THE FACT `reproduce()` RECORDED, not a verdict word this file
        # recognises. `inconclusive` means a container ran and settled nothing;
        # deriving execution from it also caught every path that returned
        # before one started, so a mutated, non-executable or empty artifact
        # was filed as `EvidenceKind.repro` -- evidence of a run that never
        # happened. That is the conflation Task 8 removed one layer out.
        executed = bool(reproduction["executed"])
        data = {
            "grade": str(grade),
            "grade_reason": why,
            "observation": candidate.get("observation"),
            "root_cause_hypothesis": candidate.get("root_cause_hypothesis"),
            "alternative_explanations": list(
                candidate.get("alternative_explanations") or []
            ),
            "failure_scenario": candidate.get("failure_scenario"),
            "declared_severity": declared,
            # Recorded so "the model said 0.95 and was wrong" is answerable
            # later. Never consulted -- `grade.py` deletes its own parameter.
            "model_confidence": candidate.get("confidence"),
            "hunt_provenance": candidate.get("provenance"),
            "reproduction": {
                "verdict": reproduction["verdict"],
                "executed": executed,
                "reproduced": reproduction["reproduced"],
                "has_runnable_artifact": reproduction["has_runnable_artifact"],
                "provenance": reproduction.get("provenance"),
                "payload": reproduction.get("payload"),
            },
            "falsify": {
                "challenged": verdict["challenged"],
                "confirmed": verdict["confirmed"],
                "strongest_counterargument": verdict["strongest_counterargument"],
                "reasoning": verdict["reasoning"],
                "remaining_uncertainty": verdict["remaining_uncertainty"],
                "severity_adjustment": verdict["severity_adjustment"],
                "provenance": verdict.get("provenance"),
            },
        }
        detail = "\n\n".join(
            part
            for part in (
                why,
                str(candidate.get("observation") or ""),
                str(candidate.get("root_cause_hypothesis") or ""),
                f"Strongest case against: {verdict['strongest_counterargument']}",
            )
            if part
        )
        try:
            return Candidate(
                lens=self.name,
                rule_id=_RULE_ID,
                subject=subject,
                title=str(candidate.get("title")),
                detail=detail,
                severity=severity,
                evidence=Evidence(
                    # `repro` only when the controller actually executed
                    # something. A finding whose evidence was written and never
                    # run is a critique wearing a repro's name.
                    kind=EvidenceKind.repro if executed else EvidenceKind.critique,
                    summary=f"{grade}: {candidate.get('title')}",
                    data=data,
                ),
                # ALSO on the field, not only in `data` above. The store
                # persists the field; nothing anywhere reads `evidence.data`.
                # Task 10 measured what the blob-only version costs: the grade
                # reached no column, no filter and no default, so the finding
                # the falsifier killed printed exactly like the one it
                # confirmed. `data` keeps its copy because the evidence blob is
                # the record of what the stages produced, and a test above
                # asserts the two cannot drift apart.
                grade=grade,
                grade_reason=why,
            )
        except LensError as exc:
            # Every string in `data` came from a model, so a field the store
            # cannot hold is the expected case rather than an exotic one. The
            # run must not die on one candidate.
            ctx.skip(
                f"code-defects [{subject}]: the finding could not be recorded "
                f"({exc})"
            )
            return None

    def _write_cost_record(self, ctx: RunContext, budget: Budget) -> None:
        """Per-stage cost against this run, for the estimator to be fit to.

        A file under the state directory rather than a table: the store has no
        migration path yet, so a new table would refuse every database written
        by an earlier build. Skipped entirely when nothing was spent -- an empty
        record would read as a run that cost nothing rather than one that never
        called a model.
        """
        if not budget.ledger:
            return
        record = {
            "run_id": ctx.run_id,
            "lens": self.name,
            "tier": ctx.tier,
            "ceiling_usd": budget.ceiling_usd,
            "spent_usd": budget.spent_usd,
            "tokens": budget.tokens,
            "calls": budget.calls,
            "unmeasured_calls": budget.unmeasured_calls,
            "stages": [entry.as_dict() for entry in budget.ledger],
        }
        path = ctx.state_root / "costs" / f"{ctx.run_id}.json"
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(record, indent=2), encoding="utf-8")
        except OSError as exc:
            ctx.skip(
                f"code-defects: this run's per-stage cost could not be written to "
                f"{path.name} ({exc}), so ${budget.spent_usd:.4f} across "
                f"{budget.calls} model call(s) was spent and not recorded."
            )
        if budget.unmeasured_calls:
            ctx.skip(
                f"code-defects: {budget.unmeasured_calls} of {budget.calls} model "
                f"call(s) reported no cost at all, so the ceiling was enforced "
                f"against a total known to be short."
            )

    # -- odds and ends -----------------------------------------------------------

    def _resolve_provider(self) -> Provider:
        if self._provider is not None:
            return self._provider
        # Imported here rather than at module scope: `provider/registry.py`
        # instantiates the CLI provider at import time, and the lens registry
        # imports this module during `whetstone --help`.
        from ...provider.registry import get_provider

        return get_provider(self.provider_name or "claude-cli")

    def _sandbox_image(self, ctx: RunContext) -> str | None:
        """`lenses.code-defects.options.sandbox_image`, or None.

        A lens option rather than a top-level config key: it is this pack's
        vocabulary, and `LensConfig.options` exists so the spine does not have
        to enumerate what a pack invents.
        """
        image = ctx.options.get("sandbox_image")
        if image is None:
            return None
        if not isinstance(image, str) or not image.strip():
            ctx.skip(
                f"code-defects: the `sandbox_image` option is {image!r}, which is "
                f"not an image name. No reproduction will be executed."
            )
            return None
        return image

    def _report_budget_gaps(self, ctx: RunContext) -> None:
        if self.ceiling_usd is None:
            ctx.skip(
                "code-defects: no `budget.ceiling.usd_per_run` is set, so this "
                "run had no cost ceiling and nothing could stop it. Every stage "
                "of this lens is a paid model call."
            )
        if self.calls_per_day is not None:
            ctx.skip(
                f"code-defects: `budget.ceiling.calls_per_day` is set to "
                f"{self.calls_per_day} and is NOT enforced -- Whetstone keeps no "
                f"cross-run call accounting yet, so a daily limit cannot be "
                f"applied. Only `usd_per_run` bounds this run."
            )
