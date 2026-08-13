"""What a finding is worth, decided by code.

INVARIANT 2 LIVES HERE. `model_confidence` is a parameter and is never read.
It is accepted so callers have somewhere honest to put it -- it is recorded for
calibration, so that "the model said 0.95 and was wrong" is answerable later --
and it is excluded from every branch below on purpose. A grade that moves with
the model's opinion of itself is the model grading itself with extra steps.

INVARIANT 3 LIVES HERE TOO. A finding with no runnable artifact is capped at B
however good the rest looks. Prose cannot close a loop: the whole point of the
milestone is that what reaches a human has been executed, not argued.

The reason string is not decoration. Every path returns a sentence naming what
lowered the grade, because a decision that reaches a user without its reasons
is a decision they cannot check.
"""

from __future__ import annotations

from enum import StrEnum


class Grade(StrEnum):
    """Best to worst, in declaration order, so callers can sort on it.

    A -- reproduced, executable, survived falsification.
    B -- survived, but nothing runnable backs it.
    C -- not reproduced.
    D -- the falsifier killed it.
    """

    A = "A"
    B = "B"
    C = "C"
    D = "D"


def grade_finding(
    *,
    reproduced: bool,
    has_runnable_artifact: bool,
    falsifier_confirmed: bool,
    alternative_explanations: int,
    model_confidence: float | None,
) -> tuple[Grade, str]:
    """Grade one finding, and say why.

    Keyword-only on purpose: five arguments of which four are booleans is
    exactly the signature where a positional swap is silent and catastrophic.

    `model_confidence` is DELIBERATELY UNUSED. See the module docstring; the
    test suite asserts both the grade and the reason are identical across the
    full range including None.
    """
    del model_confidence  # recorded by the caller, never consulted here

    # The falsifier is the last word. A finding it killed is dead however well
    # it reproduced and whatever artifact backs it -- otherwise the falsify
    # stage is advisory, and an advisory falsifier is not one.
    if not falsifier_confirmed:
        return (
            Grade.D,
            "graded D: the falsifier did not confirm the finding, which "
            "overrides everything else about it.",
        )

    if not reproduced:
        return (
            Grade.C,
            "graded C: the finding was not reproduced, so nothing here has "
            "been shown to happen.",
        )

    if not has_runnable_artifact:
        return (
            Grade.B,
            "graded B: the finding was reproduced and survived falsification, "
            "but carries no runnable artifact, so nothing can re-check it "
            "without a human repeating the work.",
        )

    reasons = [
        "reproduced",
        "backed by a runnable artifact",
        "survived falsification",
    ]
    if alternative_explanations <= 0:
        # Not a cap: a hunter that named no alternative has not been shown
        # wrong, it has been shown incurious. Worth saying, not worth
        # downgrading, and the schema requires at least one anyway.
        reasons.append(
            "though the hunter offered no alternative explanation to rule out"
        )
    return Grade.A, "graded A: " + ", ".join(reasons) + "."
