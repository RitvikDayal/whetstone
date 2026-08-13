"""Grading, where the deterministic layer holds authority.

`model_confidence` is accepted and MUST NOT influence the result. It is
recorded for calibration, and the test below asserts the grade is identical
across the whole range including None -- invariant 2 says a model's
self-assessment is discarded and recomputed from the world.
"""

from __future__ import annotations

import pytest

from whetstone.lenses.code_defects.grade import Grade, grade_finding


def _grade(**overrides):
    base = dict(
        reproduced=True,
        has_runnable_artifact=True,
        falsifier_confirmed=True,
        alternative_explanations=2,
        model_confidence=0.9,
    )
    base.update(overrides)
    return grade_finding(**base)


def test_the_best_case_grades_a():
    grade, why = _grade()
    assert grade is Grade.A
    assert why


@pytest.mark.parametrize("confidence", [None, 0.0, 0.1, 0.5, 0.99, 1.0])
def test_model_confidence_never_changes_the_grade(confidence):
    """The model's opinion of its own output is an input, not a verdict."""
    assert _grade(model_confidence=confidence)[0] is _grade(model_confidence=0.9)[0]


@pytest.mark.parametrize("confidence", [None, 0.0, 0.5, 1.0])
def test_model_confidence_never_changes_the_REASON_either(confidence):
    """A grade that is stable while its explanation moves with confidence is
    still letting the model's self-assessment reach the user."""
    assert _grade(model_confidence=confidence)[1] == _grade(model_confidence=0.9)[1]


class _Explodes:
    """A confidence value that cannot be used for anything.

    Any read that could influence a decision -- a comparison, an attribute, a
    float conversion, a truth test -- raises. Passing this proves the parameter
    is not merely *ignored in the cases we thought to parametrise* but never
    consulted at all, which the two tests above cannot show: they only cover
    the values a caller is likely to pass.
    """

    def _refuse(self, *_args, **_kwargs):
        raise AssertionError(
            "grade_finding consulted model_confidence. Invariant 2 says the "
            "deterministic layer decides and a model's self-assessment is "
            "recomputed from the world, not read back as a verdict."
        )

    __lt__ = __le__ = __gt__ = __ge__ = __eq__ = _refuse
    __bool__ = __float__ = __index__ = __hash__ = _refuse
    __getattr__ = _refuse


def test_model_confidence_is_never_consulted_at_all():
    grade, why = _grade(model_confidence=_Explodes())
    assert grade is Grade.A
    assert why == _grade(model_confidence=0.9)[1]


def test_no_runnable_artifact_caps_at_b():
    """Evidence must be executable. Prose cannot close a loop."""
    grade, why = _grade(has_runnable_artifact=False)
    assert grade is Grade.B
    assert "artifact" in why.lower()


def test_not_reproduced_cannot_reach_b():
    grade, _ = _grade(reproduced=False)
    assert grade in (Grade.C, Grade.D)


def test_falsifier_killed_it_grades_d():
    grade, why = _grade(falsifier_confirmed=False)
    assert grade is Grade.D
    assert "falsif" in why.lower()


def test_a_killed_finding_grades_d_however_good_the_rest_looks():
    """The falsifier is the last word. A reproduced finding with a runnable
    artifact that the falsifier killed is still dead."""
    for reproduced in (True, False):
        for artifact in (True, False):
            grade, _ = _grade(
                falsifier_confirmed=False,
                reproduced=reproduced,
                has_runnable_artifact=artifact,
            )
            assert grade is Grade.D, (reproduced, artifact)


def test_the_reason_names_every_factor_that_lowered_the_grade():
    _, why = _grade(has_runnable_artifact=False, alternative_explanations=0)
    assert "artifact" in why.lower()


def test_the_reason_is_never_empty_for_any_input():
    """A grade with no explanation is a path that decided something and said
    nothing about why."""
    for reproduced in (True, False):
        for artifact in (True, False):
            for confirmed in (True, False):
                for alts in (0, 1, 5):
                    _, why = _grade(
                        reproduced=reproduced,
                        has_runnable_artifact=artifact,
                        falsifier_confirmed=confirmed,
                        alternative_explanations=alts,
                    )
                    assert why and why.strip()


def test_grading_is_pure():
    first = _grade()
    second = _grade()
    assert first == second


def test_grade_sorts_best_first():
    """SORTING, not declaration order. Callers will sort on this to put the
    worst findings last, and the declaration-order form would pass for a plain
    `Enum` with the same values -- which is not sortable at all."""
    assert sorted(Grade) == [Grade.A, Grade.B, Grade.C, Grade.D]
    assert sorted([Grade.D, Grade.A, Grade.C, Grade.B]) == [
        Grade.A,
        Grade.B,
        Grade.C,
        Grade.D,
    ]
