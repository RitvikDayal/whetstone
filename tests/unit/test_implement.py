"""The implement stage: the first stage in this project permitted to write.

WHAT IS WORTH ATTACKING HERE is different from every other stage. The others
must be shown not to write; this one must be shown not to *claim* it wrote.
A summary describing a fix is the cheapest thing a model can produce and the
hardest artefact to check by eye, so the tests below concentrate on the paths
where a payload looks perfectly well-formed and nothing happened.

The mutation check inverts here, and these tests pin that inversion: everywhere
else a mutation discards the result, and here its ABSENCE does.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from whetstone.lenses.base import RunContext
from whetstone.lenses.code_defects.implement import (
    _substitutions,
    changed_files,
    implement,
)
from whetstone.lenses.code_defects.prompts import load_prompt
from whetstone.provider.base import StageRequest, StageResult, Usage

_CANDIDATE = {
    "subject": "orders.py:9",
    "title": "division by zero on an empty list",
    "observation": "average_price divides by len(prices) with no guard.",
    "root_cause_hypothesis": "No empty check before the division.",
    "failure_scenario": "average_price([]) raises ZeroDivisionError.",
    "severity": "medium",
}

_REPRODUCTION = {
    "executed": True,
    "verdict": "reproduced",
    "reproduced": True,
    "has_runnable_artifact": True,
    "artifact": {"kind": "pytest", "content": "def test_reproduces():\n    pass\n"},
}

_VERDICT = {
    "confirmed": True,
    "challenged": True,
    "strongest_counterargument": "Callers may guarantee a non-empty list.",
}


class _FakeProvider:
    name = "fake"

    def __init__(self, *results: StageResult) -> None:
        self._results = list(results)
        self.requests: list[StageRequest] = []

    def run_stage(self, request: StageRequest) -> StageResult:
        self.requests.append(request)
        return self._results.pop(0)


def _payload(**overrides) -> dict:
    base = {
        "changed_files": ["orders.py", "test_orders.py"],
        "summary": "Return 0.0 for an empty list.",
        "regression_test": {"path": "test_orders.py", "test_name": "test_empty"},
        "notes": None,
    }
    base.update(overrides)
    return base


def _result(*, mutation: str | None = "M orders.py", ok: bool = True, **overrides):
    base = dict(
        ok=ok,
        data=_payload(),
        raw="{}",
        usage=Usage(cost_usd=0.01, input_tokens=100),
        error=None,
        turns=3,
        denials=(),
        mutation=mutation,
    )
    base.update(overrides)
    return StageResult(**base)


def _ctx(tmp_path: Path) -> RunContext:
    return RunContext(
        project_root=tmp_path,
        state_root=tmp_path / "state",
        files=(),
        tier="deep",
        lens_options={},
        run_id="r1",
    )


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    root = tmp_path / "worktree"
    root.mkdir()
    subprocess.run(["git", "init", "-b", "main", "-q"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=root, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=root, check=True)
    (root / "orders.py").write_text("x = 1\n", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=root, check=True)
    subprocess.run(
        ["git", "commit", "-m", "init", "--no-gpg-sign", "-q"], cwd=root, check=True
    )
    return root


# --- the inversion: no mutation means nothing happened -------------------------


def test_a_stage_that_changed_nothing_is_not_an_implementation(tmp_path, tree):
    """The failure mode a prose payload hides best.

    Every other stage discards its result when the worktree moved. This one
    discards it when the worktree did NOT -- a well-formed summary describing a
    fix, with an untouched tree, is a description of work that did not happen.
    """
    provider = _FakeProvider(_result(mutation=None))
    outcome, skips = implement(
        _CANDIDATE, _REPRODUCTION, _VERDICT, tree, _ctx(tmp_path), provider
    )
    assert outcome["implemented"] is False
    assert any("untouched" in s for s in skips)


def test_a_real_change_is_an_implementation(tmp_path, tree):
    (tree / "orders.py").write_text("x = 2\n", encoding="utf-8")
    provider = _FakeProvider(_result())
    outcome, _ = implement(
        _CANDIDATE, _REPRODUCTION, _VERDICT, tree, _ctx(tmp_path), provider
    )
    assert outcome["implemented"] is True
    assert outcome["summary"] == "Return 0.0 for an empty list."


# --- a fix with nothing to re-check it is not acted on --------------------------


def test_no_regression_test_means_the_fix_is_not_carried(tmp_path, tree):
    """Invariant 3 one layer up from the reproduction: evidence must be
    executable, and a fix nobody can re-check is not evidence."""
    (tree / "orders.py").write_text("x = 2\n", encoding="utf-8")
    provider = _FakeProvider(_result(data=_payload(regression_test=None, notes="unsure")))
    outcome, skips = implement(
        _CANDIDATE, _REPRODUCTION, _VERDICT, tree, _ctx(tmp_path), provider
    )
    assert outcome["implemented"] is False
    assert any("regression test" in s for s in skips)
    assert any("unsure" in s for s in skips), "the stated reason must reach the user"


# --- what the stage may touch ----------------------------------------------------


def test_the_stage_runs_in_the_worktree_not_the_project(tmp_path, tree):
    """The only stage where cwd and project_root differ, and the whole safety
    story rests on which one it gets."""
    (tree / "orders.py").write_text("x = 2\n", encoding="utf-8")
    provider = _FakeProvider(_result())
    implement(_CANDIDATE, _REPRODUCTION, _VERDICT, tree, _ctx(tmp_path), provider)

    request = provider.requests[0]
    assert request.cwd == tree
    assert request.cwd != tmp_path


def test_the_stage_may_write_and_still_has_no_shell(tmp_path, tree):
    (tree / "orders.py").write_text("x = 2\n", encoding="utf-8")
    provider = _FakeProvider(_result())
    implement(_CANDIDATE, _REPRODUCTION, _VERDICT, tree, _ctx(tmp_path), provider)

    permissions = provider.requests[0].permissions
    assert {"Edit", "Write"} <= permissions.available_tools
    assert "Bash" not in permissions.available_tools
    assert "Agent" not in permissions.available_tools
    assert permissions.write_root == tree


# --- the payload's file list is a claim, not a fact -------------------------------


def test_the_changed_file_list_comes_from_git_not_from_the_payload(tmp_path, tree):
    """A stage under-reporting its own writes is exactly what must not be
    taken on trust -- and it is the shape a fix that quietly edits something
    else would take."""
    (tree / "orders.py").write_text("x = 2\n", encoding="utf-8")
    (tree / "sneaky.py").write_text("import os\n", encoding="utf-8")
    provider = _FakeProvider(_result(data=_payload(changed_files=["orders.py"])))

    outcome, skips = implement(
        _CANDIDATE, _REPRODUCTION, _VERDICT, tree, _ctx(tmp_path), provider
    )
    assert "sneaky.py" in outcome["changed_files"]
    assert "sneaky.py" not in outcome["claimed_files"]
    assert any("did not report" in s and "sneaky.py" in s for s in skips)


def test_an_honest_file_list_produces_no_complaint(tmp_path, tree):
    """The counterweight: a stage that reported what it touched must not be
    accused of hiding something, or the warning above becomes noise."""
    (tree / "orders.py").write_text("x = 2\n", encoding="utf-8")
    provider = _FakeProvider(_result(data=_payload(changed_files=["orders.py"])))
    _, skips = implement(
        _CANDIDATE, _REPRODUCTION, _VERDICT, tree, _ctx(tmp_path), provider
    )
    assert not any("did not report" in s for s in skips)


def test_changed_files_sees_new_files_not_just_modified_ones(tree):
    """A regression test is a NEW file, which is the ordinary shape of a fix
    here -- and the git default collapses a new directory to its name."""
    (tree / "tests").mkdir()
    (tree / "tests" / "test_new.py").write_text("def test_x(): pass\n", encoding="utf-8")
    found = changed_files(tree)
    assert "tests/test_new.py" in found


def test_changed_files_on_a_clean_tree_is_empty(tree):
    assert changed_files(tree) == []


# --- refusals ---------------------------------------------------------------------


def test_a_refused_tool_discards_the_change(tmp_path, tree):
    """A partial fix is worse than none, because it looks like a fix."""
    (tree / "orders.py").write_text("x = 2\n", encoding="utf-8")
    provider = _FakeProvider(_result(denials=("Write",)))
    outcome, skips = implement(
        _CANDIDATE, _REPRODUCTION, _VERDICT, tree, _ctx(tmp_path), provider
    )
    assert outcome["implemented"] is False
    assert any("refused" in s for s in skips)


def test_a_stage_that_did_not_run_says_so(tmp_path, tree):
    provider = _FakeProvider(_result(ok=False, data=None, error="binary missing"))
    outcome, skips = implement(
        _CANDIDATE, _REPRODUCTION, _VERDICT, tree, _ctx(tmp_path), provider
    )
    assert outcome["implemented"] is False
    assert any("binary missing" in s for s in skips)


def test_a_success_with_no_payload_cannot_be_constructed():
    """There is no test for that branch because there is no way to reach it.

    `StageResult.__post_init__` refuses `ok=True` with `data=None` outright, so
    the guard in `implement()` is unreachable today. It stays anyway, mirroring
    the identical guard in `falsify.py`, for the reason that file already
    states about `result.error`: the code should not fall apart the day that
    constructor guarantee moves. Asserting the refusal here is the honest
    version -- it pins WHY the branch has no test.
    """
    with pytest.raises(ValueError, match="must carry data"):
        _result(data=None)


# --- the prompt --------------------------------------------------------------------


def test_every_placeholder_in_the_prompt_is_supplied():
    """The guard that bites, as on every other stage: a `$name` added to the
    markdown with no substitution is a `KeyError` at run time, after the money
    is spent."""
    import re

    placeholders = set(re.findall(r"\$(\w+)", load_prompt("implement")))
    assert placeholders <= set(_substitutions(_CANDIDATE, _REPRODUCTION, _VERDICT))


def test_the_implementer_is_told_not_to_touch_the_reproduction(tmp_path, tree):
    """A fix that works by deleting the thing that measures it is the obvious
    attack, and the prompt is where it is closed for the model's own choices;
    verification closes it for real."""
    facts = _substitutions(_CANDIDATE, _REPRODUCTION, _VERDICT)
    assert "Do not edit or delete" in facts["reproduction"]
    assert "test_reproduces" in facts["reproduction"]


def test_the_implementer_gets_the_hypothesis_unlike_the_falsifier(tmp_path):
    """Deliberate asymmetry. The falsifier is denied the hunter's hypothesis to
    prevent anchoring; the implementer is fixing the cause, and withholding the
    cause from the thing asked to fix it would be theatre."""
    facts = _substitutions(_CANDIDATE, _REPRODUCTION, _VERDICT)
    assert "no guard" in facts["observation"]
