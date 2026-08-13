"""The reproduce stage.

THE EXECUTOR IS NEVER MOCKED HERE. Every test that exercises a verdict runs a
real `pytest` in a real temporary project through the real `run_shell`. Mocking
it would restore exactly the defect the controller-execution amendment exists
to prevent: the predecessor recorded a verdict about a defect none of its seven
runs could execute against.

The exit-code convention is the plan's, and it is the opposite of a regression
test: the artifact PASSES while the defect is present. So exit 0 is evidence,
and a failure is either absence or a broken harness -- which is what the marker
is for.
"""

from __future__ import annotations

import re
import shutil
import sys
from pathlib import Path

import pytest

from whetstone.lenses.base import RunContext
from whetstone.lenses.code_defects import reproduce as reproduce_module
from whetstone.lenses.code_defects.prompts import load_prompt
from whetstone.lenses.code_defects.reproduce import REPRO_MARKER, _sanitise, reproduce
from whetstone.provider.base import StageRequest, StageResult, Usage

_CANDIDATE = {
    "subject": "app.py:2",
    "title": "add() raises on empty input",
    "observation": "add() indexes values[0] with no length check.",
    "root_cause_hypothesis": "The caller does not guard against an empty list.",
    "alternative_explanations": ["Callers may guarantee a non-empty list."],
    "failure_scenario": "add([]) raises IndexError.",
    "severity": "high",
    "confidence": 0.8,
    "provenance": {"angle": "error handling", "turns": 5, "read_nothing": False},
}

# The canonical form the prompt teaches: try/except, with a `pytest.fail` the
# controller can attribute on the no-defect path.
#
# `with pytest.raises(...)` is deliberately NOT used. Against a fixed
# implementation it fails with pytest's own "DID NOT RAISE" message, which
# carries no marker -- so absence was unreachable through the very example the
# prompt gave, and every fixed defect would have read as a broken test.
_PASSES = (
    "import pytest\n"
    "from app import add\n\n"
    "def test_reproduces():\n"
    "    try:\n"
    "        add([])\n"
    "    except IndexError:\n"
    "        return\n"
    "    pytest.fail('WHETSTONE-REPRO: add([]) returned instead of raising')\n"
)
# Fails with the marker in its ASSERTION MESSAGE, which is what earns absence.
#
# Deliberately independent of `app`: the first version called `add([])`, which
# on the buggy fixture raises IndexError before the assertion runs -- so the
# failure message was "IndexError: list index out of range", carried no marker,
# and the verdict was correctly `inconclusive`. The fixture was wrong, not the
# code. This one isolates the property under test from the app's semantics.
_FAILS_WITH_MARKER = (
    "def test_reproduces():\n"
    "    assert False, 'WHETSTONE-REPRO: the defect did not occur'\n"
)
_FAILS_WITHOUT_MARKER = (
    "def test_reproduces():\n"
    "    import does_not_exist_anywhere\n"
    "    assert does_not_exist_anywhere\n"
)
_COLLECTS_NOTHING = "# no tests here at all\n"


class _FakeProvider:
    name = "fake"

    def __init__(self, *results: StageResult) -> None:
        self._results = list(results)
        self.requests: list[StageRequest] = []

    def run_stage(self, request: StageRequest) -> StageResult:
        self.requests.append(request)
        return self._results.pop(0)


def _payload(*, artifact: dict | None, reproduced: bool = True) -> dict:
    return {
        "reproduced": reproduced,
        "steps": ["call add([])"],
        "expected": "returns 0",
        "actual": "raises IndexError",
        "artifact": artifact,
        "environment": None,
        "notes": None,
    }


def _ok(data: dict, **overrides) -> StageResult:
    base = dict(
        ok=True,
        data=data,
        raw="{}",
        usage=Usage(input_tokens=5, output_tokens=5, cost_usd=0.04, source="usage"),
        error=None,
        turns=4,
    )
    base.update(overrides)
    return StageResult(**base)


@pytest.fixture
def ctx(tmp_path):
    """A real project with a real defect and a real test command."""
    (tmp_path / "app.py").write_text(
        "def add(values):\n    return values[0]\n", encoding="utf-8"
    )
    return RunContext(
        project_root=tmp_path,
        state_root=tmp_path / ".whetstone",
        files=(Path("app.py"),),
        tier="deep",
        lens_options={"options": {}},
        run_id="r1",
    )


# `-p no:cacheprovider` so the run leaves no .pytest_cache for the sentinel to
# report, and `-q` because the marker search reads the report.
_TEST_COMMAND = f'"{sys.executable}" -m pytest -q -p no:cacheprovider'

pytestmark = pytest.mark.skipif(
    shutil.which(sys.executable) is None, reason="no interpreter to run pytest with"
)


# --- sanitisation ---------------------------------------------------------------


def test_the_reproducer_sees_facts_and_not_opinions():
    """EXACT SET, not a list of `not in` assertions.

    A new hypothesis-shaped key added to the hunt schema later must FAIL this
    test rather than pass it by not being on somebody's deny-list. That is the
    difference between a rule and a list of the cases someone thought of.
    """
    assert set(_sanitise(_CANDIDATE)) == {"subject", "observation", "failure_scenario"}


def test_sanitisation_survives_a_candidate_missing_optional_keys():
    assert _sanitise({"subject": "a.py", "observation": "o"})["failure_scenario"] is None


# --- what the controller decides ------------------------------------------------


def test_a_passing_artifact_is_the_defect_reproduced(ctx):
    """Exit 0. The artifact asserts the broken behaviour, so passing IS the
    evidence -- the opposite of a regression test, deliberately."""
    provider = _FakeProvider(
        _ok(_payload(artifact={"kind": "pytest", "content": _PASSES}))
    )
    result, skips = reproduce(_CANDIDATE, ctx, provider, _TEST_COMMAND)

    assert result["reproduced"] is True
    assert result["verdict"] == "reproduced"
    assert skips == []


def test_the_controller_overrides_a_model_that_claimed_reproduction(ctx):
    """`reproduced` in the payload is a claim. The exit code is what happened.
    Seven runs on the predecessor recorded a verdict about a defect none of
    them could execute against."""
    provider = _FakeProvider(
        _ok(
            _payload(
                artifact={"kind": "pytest", "content": _FAILS_WITH_MARKER},
                reproduced=True,
            )
        )
    )
    result, _ = reproduce(_CANDIDATE, ctx, provider, _TEST_COMMAND)

    assert result["reproduced"] is False, "the model's claim survived execution"
    assert result["verdict"] == "absent"


def test_absence_is_earned_by_the_marker_and_not_by_the_exit_code(ctx):
    """A failure without the marker is a broken harness, not evidence that the
    defect is gone. Calling it absence is how a tool closes a real defect on
    the strength of its own typo."""
    provider = _FakeProvider(
        _ok(_payload(artifact={"kind": "pytest", "content": _FAILS_WITHOUT_MARKER}))
    )
    result, skips = reproduce(_CANDIDATE, ctx, provider, _TEST_COMMAND)

    assert result["verdict"] == "inconclusive"
    assert result["reproduced"] is False
    assert any("marker" in skip for skip in skips)


def test_an_artifact_that_collects_no_tests_is_inconclusive(ctx):
    """pytest exits 5 for "no tests ran". Without this it is just another
    non-zero code, and a file with no test in it would read as absence."""
    provider = _FakeProvider(
        _ok(_payload(artifact={"kind": "pytest", "content": _COLLECTS_NOTHING}))
    )
    result, skips = reproduce(_CANDIDATE, ctx, provider, _TEST_COMMAND)

    assert result["verdict"] == "inconclusive"
    assert any("no tests" in skip for skip in skips)


# --- what the controller refuses ------------------------------------------------


@pytest.mark.parametrize("kind", ["script", "command"])
def test_only_pytest_artifacts_are_executed(kind, ctx):
    """Option A. Whetstone will not run an arbitrary command a model wrote, so
    these are refused and the finding carries no runnable artifact."""
    provider = _FakeProvider(
        _ok(_payload(artifact={"kind": kind, "content": "rm -rf /"}))
    )
    result, skips = reproduce(_CANDIDATE, ctx, provider, _TEST_COMMAND)

    assert result["has_runnable_artifact"] is False
    assert result["verdict"] == "inconclusive"
    assert any(kind in skip for skip in skips)


def test_a_null_artifact_is_an_honest_answer_not_a_failure(ctx):
    """The prompt tells the model to say so rather than fabricate. That has to
    be graded as honesty, which means it cannot also be a skip."""
    provider = _FakeProvider(_ok(_payload(artifact=None, reproduced=False)))
    result, skips = reproduce(_CANDIDATE, ctx, provider, _TEST_COMMAND)

    assert result["has_runnable_artifact"] is False
    assert result["verdict"] == "not attempted"
    assert skips == []


def test_a_provider_failure_is_a_skip(ctx):
    provider = _FakeProvider(
        StageResult(ok=False, data=None, raw="", usage=Usage(), error="CLI missing")
    )
    result, skips = reproduce(_CANDIDATE, ctx, provider, _TEST_COMMAND)

    assert result["verdict"] == "inconclusive"
    assert any("CLI missing" in skip for skip in skips)


# --- the artifact must not outlive the run --------------------------------------


def test_the_artifact_is_removed_after_the_run(ctx):
    provider = _FakeProvider(
        _ok(_payload(artifact={"kind": "pytest", "content": _PASSES}))
    )
    reproduce(_CANDIDATE, ctx, provider, _TEST_COMMAND)

    leftovers = [p.name for p in ctx.project_root.rglob("*whetstone_repro*")]
    assert leftovers == [], leftovers


def test_a_repro_that_writes_elsewhere_is_reported(ctx):
    """The sentinel runs around the controller's own execution, excluding the
    file it deliberately wrote. A reproduction that modifies the repository is
    a finding about the reproduction."""
    writer = (
        "from pathlib import Path\n\n"
        "def test_reproduces():\n"
        "    Path('side_effect.txt').write_text('written by the repro')\n"
        "    assert True, 'WHETSTONE-REPRO: ran'\n"
    )
    provider = _FakeProvider(
        _ok(_payload(artifact={"kind": "pytest", "content": writer}))
    )
    result, skips = reproduce(_CANDIDATE, ctx, provider, _TEST_COMMAND)

    assert (ctx.project_root / "side_effect.txt").exists(), "the premise: it wrote"
    assert result["mutation"] is not None
    assert any("side_effect.txt" in skip for skip in skips)


# --- what gets sent -------------------------------------------------------------


def test_the_stage_runs_under_the_reproduce_profile_with_no_shell(ctx):
    provider = _FakeProvider(_ok(_payload(artifact=None)))
    reproduce(_CANDIDATE, ctx, provider, _TEST_COMMAND)

    request = provider.requests[0]
    assert request.stage == "reproduce"
    assert "Bash" not in request.permissions.available_tools


def test_the_prompt_carries_only_the_sanitised_fields(ctx):
    """The anchoring guard, asserted at the boundary that actually sends it.
    `_sanitise` returning the right dict is worth nothing if the prompt is
    built from the raw candidate."""
    provider = _FakeProvider(_ok(_payload(artifact=None)))
    reproduce(_CANDIDATE, ctx, provider, _TEST_COMMAND)

    sent = provider.requests[0].prompt
    assert _CANDIDATE["observation"] in sent
    assert _CANDIDATE["root_cause_hypothesis"] not in sent
    assert _CANDIDATE["title"] not in sent
    assert "0.8" not in sent


def test_a_hanging_reproduction_is_killed_and_settles_nothing(ctx, monkeypatch):
    """A model-written test that never returns must not hang the run, and being
    killed is not a verdict about the code. Calling a timeout `absent` would
    close a real defect because the machine was slow."""
    monkeypatch.setattr(reproduce_module, "_TEST_TIMEOUT_SECONDS", 3)
    hangs = (
        "import time\n\n"
        "def test_reproduces():\n"
        "    time.sleep(120)\n"
        "    assert True, 'WHETSTONE-REPRO: never gets here'\n"
    )
    provider = _FakeProvider(
        _ok(_payload(artifact={"kind": "pytest", "content": hangs}))
    )
    result, skips = reproduce(_CANDIDATE, ctx, provider, _TEST_COMMAND)

    assert result["verdict"] == "inconclusive"
    assert result["reproduced"] is False
    assert any("did not finish" in skip for skip in skips)
    assert [p.name for p in ctx.project_root.rglob("*whetstone_repro*")] == []


def test_the_prompt_TEMPLATE_cannot_ask_for_anything_sanitisation_withholds():
    """The guard that matters is on the markdown, not on the code.

    `_prompt_for` names its three substitutions explicitly, so passing it the
    raw candidate changes nothing -- a mutation proved that. The reachable way
    to leak the hypothesis is to add `$root_cause_hypothesis` to the prompt
    FILE and wire it up, and this is what fails when the first half of that
    happens.
    """
    template = load_prompt("reproduce")
    placeholders = set(re.findall(r"\$(\w+)", template))
    allowed = set(_sanitise(_CANDIDATE))

    assert placeholders <= allowed, sorted(placeholders - allowed)
    assert "root_cause_hypothesis" not in placeholders
    assert "confidence" not in placeholders
    assert "severity" not in placeholders


def test_the_canonical_artifact_reaches_ABSENT_against_a_fixed_implementation(
    ctx,
):
    """The form the prompt teaches has to work in BOTH directions.

    `with pytest.raises(...)` fails with pytest's own DID NOT RAISE message
    when the defect is fixed. That carries no marker, so absence was
    unreachable through the very example the prompt gave -- every fixed defect
    would have read as a broken test.
    """
    (ctx.project_root / "app.py").write_text(
        "def add(values):\n    return values[0] if values else 0\n", encoding="utf-8"
    )
    provider = _FakeProvider(
        _ok(_payload(artifact={"kind": "pytest", "content": _PASSES}))
    )
    result, skips = reproduce(_CANDIDATE, ctx, provider, _TEST_COMMAND)

    assert result["verdict"] == "absent", skips
    assert result["reproduced"] is False


def test_printing_the_marker_does_not_buy_absence(ctx):
    """THE ATTACK the output scan allowed.

    The artifact prints `WHETSTONE-REPRO` and then fails an unrelated
    assertion. Exit 1 plus the string somewhere in stdout used to read as
    absence, although the predicate meant to check the defect was never
    checked. The marker is bound to the FAILING TEST now, via pytest's own
    report rather than the model's output.
    """
    smuggles = (
        "def test_reproduces():\n"
        "    print('WHETSTONE-REPRO: nothing to see here')\n"
        "    assert 1 == 2, 'an unrelated assertion'\n"
    )
    provider = _FakeProvider(
        _ok(_payload(artifact={"kind": "pytest", "content": smuggles}))
    )
    result, skips = reproduce(_CANDIDATE, ctx, provider, _TEST_COMMAND)

    assert result["verdict"] == "inconclusive", skips
    assert any("marker" in skip for skip in skips)


def test_more_than_one_test_cannot_attribute_a_failure(ctx):
    """Two tests, one failing with the marker. Which defect did that settle?
    Nothing here can say, so it settles nothing."""
    two = (
        "def test_one():\n"
        "    assert True\n\n"
        "def test_two():\n"
        "    assert False, 'WHETSTONE-REPRO: the wrong one'\n"
    )
    provider = _FakeProvider(_ok(_payload(artifact={"kind": "pytest", "content": two})))
    result, skips = reproduce(_CANDIDATE, ctx, provider, _TEST_COMMAND)

    assert result["verdict"] == "inconclusive"
    assert any("rather than one" in skip for skip in skips)


def test_the_junit_report_is_not_left_in_the_project(ctx):
    """It is Whetstone's bookkeeping, so it goes under state_root -- writing it
    into the worktree would be a file we put in the user's repository."""
    provider = _FakeProvider(
        _ok(_payload(artifact={"kind": "pytest", "content": _PASSES}))
    )
    reproduce(_CANDIDATE, ctx, provider, _TEST_COMMAND)

    assert list(ctx.project_root.rglob("*.xml")) == []
    assert list(ctx.state_root.rglob("*.xml")) == []


def test_a_test_command_that_writes_no_report_cannot_produce_absence(ctx):
    """The binding degrades honestly. A declared test command that is not
    pytest -- or one that rejects `--junit-xml` -- leaves nothing for the
    controller to attribute a failure to, and an unattributable failure is not
    evidence that the defect is gone."""
    provider = _FakeProvider(
        _ok(_payload(artifact={"kind": "pytest", "content": _FAILS_WITH_MARKER}))
    )
    exits_one_writes_nothing = f'"{sys.executable}" -c "import sys; sys.exit(1)"'
    result, skips = reproduce(_CANDIDATE, ctx, provider, exits_one_writes_nothing)

    assert result["verdict"] == "inconclusive"
    assert any("no report" in skip for skip in skips)


def test_the_prompt_teaches_the_form_that_can_reach_absence():
    """The example is load-bearing, not decoration.

    `with pytest.raises(...)` fails with pytest's own DID NOT RAISE message
    against a fixed implementation. That carries no marker, so absence becomes
    unreachable through the very form the prompt recommends -- and a prompt
    that quietly regressed to it would make every fixed defect read as a broken
    test, with nothing failing to say so.
    """
    template = load_prompt("reproduce")
    assert "pytest.fail(" in template, "the example must show a message we control"
    assert REPRO_MARKER in template
    assert "Do not use `with pytest.raises(...)` for this." in template
