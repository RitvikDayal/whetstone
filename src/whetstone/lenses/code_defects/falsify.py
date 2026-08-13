"""The falsify stage: the only stage whose job is to make a finding go away.

WHY THIS ONE IS DIFFERENT. Every stage before it finds things. This is the one
that decides whether what survives is worth a human's time -- and a falsifier
that rubber-stamps is worse than no falsifier at all, because it launders a
plausible story into a confirmed finding. So every deterministic decision below
is chosen in the same direction: failing to challenge a finding must never read
as having challenged it.

WHAT IT IS NOT TOLD. Invariant 1: the falsifier never receives the hunter's
hypothesis, confidence, severity, title or alternative explanations. It gets
the observation, where it is, the scenario claimed, and what happened when the
CONTROLLER ran the reproduction. That is prevented structurally rather than
politely -- this is a separate `claude` process reading a prompt with no field
that could carry an explanation.

AND THE GUARD THAT MATTERS IS ON THE MARKDOWN, NOT ON THIS FILE. The reproduce
stage learned it the hard way: a mutation showed `_prompt_for(candidate)` and
`_prompt_for(_sanitise(candidate))` were indistinguishable, because the
substitution named its fields explicitly and the raw candidate leaked nothing.
The reachable leak is somebody adding `$root_cause_hypothesis` to `falsify.md`,
so the test that catches it parses the template's own placeholders and asserts
they are a subset of what sanitisation provides. `_substitutions` here is built
by WALKING the sanitised dict rather than by naming its fields again, which
makes the code-level guard testable too.

THE KILL REASONS LIVE IN THE PROMPT, NOT THE SCHEMA. Intended behaviour, stale
documentation, bad test data, configuration, feature flags, already fixed,
duplicate, too small to matter. As a schema enum they would become a menu to
pick from instead of a thing to think about, and would silently exclude the
reason nobody listed. The contract's only enum is the severity scale.

CONFIRMATION IS EARNED, AND FAILING TO RUN IS NOT CONFIRMING. A stage that
mutated the worktree, was refused a tool, did not run, or returned no payload
leaves `confirmed` False and records why. `challenged` is what separates "the
falsifier killed it" from "nothing ever challenged it": `confirmed=False` alone
cannot say which happened, and those two deserve opposite treatment.

A CONFIRMATION WITHOUT A COUNTERARGUMENT IS NOT ONE. The schema requires
`strongest_counterargument`, and a schema is the model's side of the claim.
Invariant 2 says that is recomputed here, so a payload whose counterargument is
blank or whitespace is discarded whatever `confirmed` says -- a falsifier that
agrees without stating the best case against the finding has agreed rather than
falsified.
"""

from __future__ import annotations

from string import Template
from typing import Any

from ...policy.profiles import profile_for
from ...provider.base import Provider, StageRequest
from ...schemas import load_schema
from ..base import RunContext
from .prompts import load_prompt

# What the controller's own verdict means, spelled out rather than handed over
# bare. Whetstone's exit-code convention is the OPPOSITE of a regression
# test's -- the artifact passes while the defect is present -- so "reproduced"
# on its own is a word a reader can take backwards, and this reader is the one
# being asked to argue the finding is nothing.
_VERDICT_MEANING: dict[str, str] = {
    "reproduced": "the controller executed the evidence and the defect happened",
    "absent": "the controller executed the evidence and the defect did NOT happen",
    "inconclusive": (
        "the controller executed the evidence and it settled nothing either way"
    ),
    "not attempted": "no reproduction could be written, so nothing was executed",
    "not executed": (
        "a reproduction was written but the controller could not run it, so "
        "nothing was executed"
    ),
}

_UNKNOWN_VERDICT = "an outcome this build does not recognise; treat it as settling nothing"


def _sanitise(candidate: dict[str, Any]) -> dict[str, Any]:
    """What the falsifier is allowed to see of the finding.

    Facts, not opinions -- the same rule the reproducer runs under. The
    observation, where it is, and the scenario claimed. Never
    `root_cause_hypothesis`, `confidence`, `severity`, `title` or
    `alternative_explanations`: those are the discoverer's conclusions, and a
    falsifier that reads them is being told what it is supposed to disprove,
    which is the same anchoring failure from the other side.

    An ALLOW-LIST, so a hypothesis-shaped field added to the hunt schema later
    is excluded by default rather than by somebody remembering to add it to a
    deny-list.
    """
    return {
        "subject": candidate.get("subject"),
        "observation": candidate.get("observation"),
        "failure_scenario": candidate.get("failure_scenario"),
    }


def _reproduction_text(reproduction: dict[str, Any]) -> str:
    """What the controller did about this finding, and what came back.

    AN ALLOW-LIST OVER THE REPRODUCE OUTCOME TOO. `verdict` was computed from
    an exit code, so it is a fact and it travels. The reproduce PAYLOAD's own
    `reproduced` field is the model's claim about itself, which the controller
    has already overwritten -- forwarding it would hand the falsifier an
    opinion invariant 2 discarded. The reproducer's free-text `notes`, its cost
    and its turn count are not evidence about the defect and do not travel
    either.

    The artifact DOES travel, and has to. "The test data is wrong, not the
    code" is one of the ways a finding like this turns out to be nothing, and a
    falsifier that cannot see the test cannot make that argument at all.
    """
    verdict = str(reproduction.get("verdict") or "inconclusive")
    lines = [
        f"The controller ran this itself; the model that wrote the reproduction "
        f"had no shell and executed nothing. Verdict: {verdict} -- "
        f"{_VERDICT_MEANING.get(verdict, _UNKNOWN_VERDICT)}."
    ]

    payload = reproduction.get("payload") or {}
    steps = [str(step) for step in (payload.get("steps") or [])]
    if steps:
        lines.append("")
        lines.append("Steps the reproducer gave:")
        lines.extend(f"- {step}" for step in steps)

    expected = payload.get("expected")
    actual = payload.get("actual")
    if expected or actual:
        lines.append("")
        lines.append(f"Expected: {expected or '(not stated)'}")
        lines.append(f"Actual: {actual or '(not stated)'}")

    artifact = payload.get("artifact") or {}
    content = str(artifact.get("content") or "").strip()
    if content:
        lines.append("")
        lines.append("What was executed:")
        lines.append("```")
        lines.append(content)
        lines.append("```")

    return "\n".join(lines)


def _substitutions(
    candidate: dict[str, Any], reproduction: dict[str, Any]
) -> dict[str, str]:
    """Exactly what may be substituted into `falsify.md`, and nothing else.

    Built by walking the sanitised dict instead of naming its fields a second
    time, so a mutation that hands this the raw candidate adds keys and is
    caught by an exact-set test. The equivalent on the reproduce stage named
    its fields and was provably immune to that mutation, which made its
    code-level guard decoration; the placeholder test on the markdown is still
    the one that closes the reachable leak.
    """
    facts = {
        key: str(value) if value not in (None, "") else "(none given)"
        for key, value in _sanitise(candidate).items()
    }
    facts["reproduction"] = _reproduction_text(reproduction)
    return facts


def _prompt_for(candidate: dict[str, Any], reproduction: dict[str, Any]) -> str:
    return Template(load_prompt("falsify")).safe_substitute(
        **_substitutions(candidate, reproduction)
    )


def falsify(
    candidate: dict[str, Any],
    reproduction: dict[str, Any],
    ctx: RunContext,
    provider: Provider,
) -> tuple[dict[str, Any], list[str]]:
    """Try to kill one finding, and believe the attempt only when it happened.

    *reproduction* is the outcome dict `reproduce()` returned, not the model's
    payload: what travels from it is the controller's own verdict, which came
    from an exit code.
    """
    skips: list[str] = []
    result = provider.run_stage(
        StageRequest(
            stage="falsify",
            prompt=_prompt_for(candidate, reproduction),
            schema=load_schema("falsify"),
            permissions=profile_for("falsify"),
            effort=str(ctx.options.get("effort", "medium")),
            # None on purpose, and measured: a per-stage ceiling low enough to
            # be useful makes the stage a guaranteed no-op. Task 9's budget is
            # run-level and stops between stages.
            max_budget_usd=None,
            cwd=ctx.project_root,
        )
    )

    outcome: dict[str, Any] = {
        # The safe direction on every path below. An unchallenged finding must
        # never reach a user wearing a confirmation.
        "confirmed": False,
        # Whether a challenge happened at all. See the module docstring: this
        # is what stops "the falsifier killed it" and "nothing looked at it"
        # from being the same value.
        "challenged": False,
        "strongest_counterargument": None,
        "reasoning": None,
        "remaining_uncertainty": [],
        "severity_adjustment": None,
        "mutation": result.mutation,
        "payload": result.data,
        "provenance": {
            "turns": result.turns,
            "cost_usd": result.usage.cost_usd,
            # total_tokens, never input_tokens: measured 4 against 41,036 on
            # one call.
            "tokens": result.usage.total_tokens,
        },
    }

    # First and unconditional, as in every other stage. A read-only stage that
    # wrote is not a stage whose verdict means anything, and its payload can
    # look perfectly well-formed while it happens.
    if result.mutation:
        skips.append(
            f"falsify modified the worktree and its verdict was discarded: "
            f"{result.mutation}"
        )
        return outcome, skips
    if result.denials:
        skips.append(
            f"falsify was refused {', '.join(sorted(set(result.denials)))} and "
            f"answered on less than it asked for, so its verdict was discarded."
        )
        return outcome, skips
    # Two conditions, two reasons -- `StageResult` guarantees a failure carries
    # an error, and reading `result.error` regardless would print "did not run:
    # None" the day that guarantee moves.
    if not result.ok:
        skips.append(
            f"falsify did not run: "
            f"{result.error or 'the provider failed without saying why'}"
        )
        return outcome, skips
    if result.data is None:
        skips.append(
            "falsify returned success with no payload, so there is nothing to read."
        )
        return outcome, skips

    counterargument = str(result.data.get("strongest_counterargument") or "").strip()
    if not counterargument:
        skips.append(
            "falsify returned no counterargument, so it agreed rather than "
            "falsified and its verdict was discarded."
        )
        return outcome, skips

    outcome["challenged"] = True
    outcome["confirmed"] = bool(result.data.get("confirmed"))
    outcome["strongest_counterargument"] = counterargument
    outcome["reasoning"] = result.data.get("reasoning")
    outcome["remaining_uncertainty"] = list(result.data.get("remaining_uncertainty") or [])
    outcome["severity_adjustment"] = result.data.get("severity_adjustment")
    return outcome, skips
