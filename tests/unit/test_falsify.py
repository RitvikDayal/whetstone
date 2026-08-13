"""The falsify stage.

THE DIFFERENTIATOR, AND THEREFORE THE ONE MOST WORTH ATTACKING. Every other
stage finds things; this is the one that decides whether what survives is worth
a human's time. A falsifier that rubber-stamps is worse than none, because it
launders a plausible story into a confirmed finding -- so the tests below are
weighted towards the ways a confirmation could be produced by something other
than an actual challenge.

Nothing here needs a container: the falsify stage executes nothing. What it
must be shown to do is refuse to confirm on every path where the challenge did
not really happen, and to carry the counterargument through on the path where
it did.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from whetstone.lenses.base import RunContext
from whetstone.lenses.code_defects.falsify import (
    _reproduction_text,
    _sanitise,
    _substitutions,
    falsify,
)
from whetstone.lenses.code_defects.prompts import load_prompt
from whetstone.provider.base import StageRequest, StageResult, Usage
from whetstone.schemas import load_schema

# Withheld fields get unique sentinels rather than realistic-looking values
# like "high" or "0.8" -- those could coincidentally appear in unrelated
# prompt prose and pass the leakage assertion by accident rather than by
# actually proving nothing leaked.
_WITHHELD_SEVERITY = "ZZ-SEVERITY-SHOULD-NOT-TRAVEL"
_WITHHELD_CONFIDENCE = "ZZ-CONFIDENCE-SHOULD-NOT-TRAVEL"

_CANDIDATE = {
    "subject": "app.py:2",
    "title": "add() raises on empty input",
    "observation": "add() indexes values[0] with no length check.",
    "root_cause_hypothesis": "The caller does not guard against an empty list.",
    "alternative_explanations": ["Callers may guarantee a non-empty list."],
    "failure_scenario": "add([]) raises IndexError.",
    "severity": _WITHHELD_SEVERITY,
    "confidence": _WITHHELD_CONFIDENCE,
    "provenance": {"angle": "error handling", "turns": 5, "read_nothing": False},
}

_ARTIFACT_CONTENT = (
    "import pytest\n"
    "from app import add\n\n"
    "def test_reproduces():\n"
    "    try:\n"
    "        add([])\n"
    "    except IndexError:\n"
    "        return\n"
    "    pytest.fail('WHETSTONE-REPRO: add([]) returned instead of raising')\n"
)

# The reproducer's PAYLOAD claim is deliberately the opposite of the
# controller's verdict in this fixture, so any test that cannot tell them apart
# is visibly wrong rather than accidentally right.
_REPRODUCER_NOTES = "ZZ-REPRODUCER-NOTES-SHOULD-NOT-TRAVEL"


def _reproduction(verdict: str = "reproduced", *, model_claim: bool = True) -> dict:
    return {
        "reproduced": verdict == "reproduced",
        "verdict": verdict,
        "has_runnable_artifact": True,
        "mutation": None,
        "payload": {
            "reproduced": model_claim,
            "steps": ["call add([])"],
            "expected": "returns 0",
            "actual": "raises IndexError",
            "artifact": {"kind": "pytest", "content": _ARTIFACT_CONTENT},
            "environment": None,
            "notes": _REPRODUCER_NOTES,
        },
        "provenance": {"turns": 4, "cost_usd": 0.04, "tokens": 900},
    }


class _FakeProvider:
    name = "fake"

    def __init__(self, *results: StageResult) -> None:
        self._results = list(results)
        self.requests: list[StageRequest] = []

    def run_stage(self, request: StageRequest) -> StageResult:
        self.requests.append(request)
        return self._results.pop(0)


def _payload(
    *,
    confirmed: bool = True,
    counterargument: str = "The caller list is validated one frame up.",
    **overrides,
) -> dict:
    base = {
        "confirmed": confirmed,
        "strongest_counterargument": counterargument,
        "reasoning": "Read the two call sites; neither validates.",
        "remaining_uncertainty": ["third-party callers were not read"],
        "severity_adjustment": None,
        "notes": None,
    }
    base.update(overrides)
    return base


def _ok(data: dict | None, **overrides) -> StageResult:
    base = dict(
        ok=True,
        data=data,
        raw="{}",
        usage=Usage(input_tokens=4, output_tokens=6, cost_usd=0.05, source="usage"),
        error=None,
        turns=6,
    )
    base.update(overrides)
    return StageResult(**base)


@pytest.fixture
def ctx(tmp_path):
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


# --- sanitisation: invariant 1, structurally ------------------------------------


def test_the_falsifier_sees_facts_and_not_opinions():
    """EXACT SET, not a series of `not in` assertions.

    An allow-list, so a hypothesis-shaped key added to the hunt schema later is
    excluded by default and FAILS this test rather than passing it by not being
    on somebody's deny-list.
    """
    assert set(_sanitise(_CANDIDATE)) == {"subject", "observation", "failure_scenario"}


def test_sanitisation_survives_a_candidate_missing_optional_keys():
    assert _sanitise({"subject": "a.py", "observation": "o"})["failure_scenario"] is None


def test_the_substitution_map_is_exactly_the_allow_list_plus_the_reproduction():
    """The mutation guard the reproduce stage could not have.

    `reproduce._prompt_for` names its three substitutions explicitly, so
    `_prompt_for(candidate)` and `_prompt_for(_sanitise(candidate))` are
    indistinguishable -- a mutation proved it, which made the code-level guard
    decoration. This map is built by WALKING the sanitised dict, so swapping in
    the raw candidate adds keys and goes red right here.
    """
    assert set(_substitutions(_CANDIDATE, _reproduction())) == {
        "subject",
        "observation",
        "failure_scenario",
        "reproduction",
    }


def test_the_prompt_TEMPLATE_cannot_ask_for_anything_sanitisation_withholds():
    """The guard that matters is on the markdown, not on the code.

    The reachable leak is somebody adding `$root_cause_hypothesis` to
    `falsify.md`. This parses the template's own placeholders and goes red the
    moment one of them names something sanitisation does not provide.
    """
    placeholders = set(re.findall(r"\$(\w+)", load_prompt("falsify")))
    assert placeholders <= set(_substitutions(_CANDIDATE, _reproduction())), sorted(
        placeholders
    )


def test_the_prompt_carries_only_the_sanitised_fields(ctx):
    """Asserted at the boundary that actually sends it, not on a helper."""
    provider = _FakeProvider(_ok(_payload()))
    falsify(_CANDIDATE, _reproduction(), ctx, provider)

    sent = provider.requests[0].prompt
    assert _CANDIDATE["observation"] in sent
    assert _CANDIDATE["root_cause_hypothesis"] not in sent
    assert _CANDIDATE["title"] not in sent
    assert _CANDIDATE["alternative_explanations"][0] not in sent
    assert _WITHHELD_CONFIDENCE not in sent
    assert _WITHHELD_SEVERITY not in sent


# --- what the falsifier is told about the reproduction --------------------------


def test_the_controllers_verdict_travels_and_the_models_claim_does_not():
    """The exit code is a fact. `payload["reproduced"]` is a self-assessment the
    controller has ALREADY overwritten -- forwarding it would hand the falsifier
    an opinion invariant 2 discarded, and in this fixture the two disagree."""
    text = _reproduction_text(_reproduction("absent", model_claim=True))

    assert "absent" in text
    assert "did NOT happen" in text
    assert "the defect happened" not in text
    assert _REPRODUCER_NOTES not in text


def test_the_executed_artifact_reaches_the_falsifier():
    """"The test data is wrong, not the code" is one of the ways this finding
    turns out to be nothing, and a falsifier that cannot see the test cannot
    make that argument at all."""
    assert _ARTIFACT_CONTENT.strip() in _reproduction_text(_reproduction())


def test_an_unrecognised_verdict_is_not_silently_read_as_success():
    text = _reproduction_text({"verdict": "banana"})
    assert "banana" in text
    assert "settling nothing" in text


def test_a_reproduction_with_no_payload_at_all_still_renders():
    """`not attempted` carries no payload. A crash here would take the falsify
    stage out entirely on the exact findings that most need challenging."""
    text = _reproduction_text({"verdict": "not attempted", "payload": None})
    assert "not attempted" in text


def test_a_missing_verdict_is_not_read_as_a_genuine_inconclusive():
    """`reproduce()` always sets `verdict`, so an absent key only reaches this
    stage through a hand-built dict -- but a default that claims execution
    happened when it did not is the exact failure class this stage exists to
    prevent. Falling back to a real verdict word ("inconclusive") would tell
    the falsifier the controller ran the evidence and it settled nothing,
    when in fact nothing ran at all."""
    text = _reproduction_text({"payload": None})
    assert "the controller executed the evidence and it settled nothing" not in text
    assert "no verdict was recorded" in text


@pytest.mark.parametrize("verdict", ["reproduced", "absent", "inconclusive"])
def test_a_verdict_that_means_execution_happened_says_the_controller_ran_it(verdict):
    """The other half of the fix: fixing the noun (the verdict word) and
    leaving the lead sentence unconditional was the miss CodeRabbit caught on
    the round before this one. These three verdicts are the ones where the
    controller genuinely ran the evidence, so the claim belongs here."""
    text = _reproduction_text({"verdict": verdict, "payload": None})
    assert "the controller ran this itself" in text.lower()


@pytest.mark.parametrize("verdict", ["not attempted", "not executed"])
def test_a_verdict_that_means_nothing_ran_never_claims_the_controller_ran_it(verdict):
    """Checked per verdict, not only for the missing-key case: a fix that
    special-cased `unstated` alone would leave this one -- a reproduction
    that WAS written but that the controller declined to run -- still lying
    about who ran what."""
    text = _reproduction_text({"verdict": verdict, "payload": None})
    lowered = text.lower()
    assert "the controller ran this itself" not in lowered
    assert "nothing here was executed" in lowered


@pytest.mark.parametrize("verdict", ["unstated", "banana"])
def test_a_verdict_that_is_unknown_or_unrecorded_makes_no_execution_claim_either_way(
    verdict,
):
    """`unstated` and an unrecognised verdict are both cases where whether
    anything ran is genuinely not known -- neither "the controller ran it"
    nor "nothing ran" is a claim this code can back, so the lead sentence
    must assert neither."""
    reproduction = {"payload": None} if verdict == "unstated" else {
        "verdict": verdict,
        "payload": None,
    }
    text = _reproduction_text(reproduction).lower()
    assert "the controller ran this itself" not in text
    assert "nothing here was executed" not in text
    assert "not recorded" in text


# --- the stage's own decisions --------------------------------------------------


def test_the_stage_runs_under_the_falsify_profile_with_no_shell(ctx):
    provider = _FakeProvider(_ok(_payload()))
    falsify(_CANDIDATE, _reproduction(), ctx, provider)

    request = provider.requests[0]
    assert request.stage == "falsify"
    assert "Bash" not in request.permissions.available_tools
    assert request.max_budget_usd is None


def test_a_confirming_verdict_still_carries_its_counterargument(ctx):
    """STEP 3. The schema requires the field; this asserts the STAGE propagates
    it, so a lens that drops it on the confirming path is caught here rather
    than discovered by a user reading a finding with no case against it."""
    counter = "Every caller is a generator that cannot yield an empty list."
    provider = _FakeProvider(_ok(_payload(confirmed=True, counterargument=counter)))
    outcome, skips = falsify(_CANDIDATE, _reproduction(), ctx, provider)

    assert outcome["confirmed"] is True
    assert outcome["challenged"] is True
    assert outcome["strongest_counterargument"] == counter
    assert skips == []


def test_a_confirmation_with_a_blank_counterargument_is_discarded(ctx):
    """A falsifier that agrees without stating the best case against the
    finding has agreed, not falsified. The schema is the model's side of the
    claim; invariant 2 says it is recomputed here."""
    provider = _FakeProvider(_ok(_payload(confirmed=True, counterargument="   ")))
    outcome, skips = falsify(_CANDIDATE, _reproduction(), ctx, provider)

    # The skip is the assertion that matters: `confirmed is False` alone would
    # also hold if this had simply never reached the check.
    assert any("counterargument" in skip for skip in skips), skips
    assert outcome["confirmed"] is False
    assert outcome["challenged"] is False


def test_a_killed_finding_is_a_challenge_that_happened(ctx):
    """`confirmed=False` and `challenged=True`. Without the second field a
    finding the falsifier killed is indistinguishable from one nothing ever
    looked at, and those deserve opposite treatment."""
    provider = _FakeProvider(_ok(_payload(confirmed=False)))
    outcome, skips = falsify(_CANDIDATE, _reproduction(), ctx, provider)

    assert outcome["confirmed"] is False
    assert outcome["challenged"] is True
    assert skips == []


def test_the_rest_of_the_payload_propagates(ctx):
    provider = _FakeProvider(
        _ok(
            _payload(
                remaining_uncertainty=["a", "b"],
                severity_adjustment="low",
                reasoning="checked both call sites",
            )
        )
    )
    outcome, _ = falsify(_CANDIDATE, _reproduction(), ctx, provider)

    assert outcome["remaining_uncertainty"] == ["a", "b"]
    assert outcome["severity_adjustment"] == "low"
    assert outcome["reasoning"] == "checked both call sites"


def test_provenance_records_total_tokens_and_not_input_tokens(ctx):
    """Measured 4 against 41,036 on one real call."""
    provider = _FakeProvider(_ok(_payload()))
    outcome, _ = falsify(_CANDIDATE, _reproduction(), ctx, provider)

    assert outcome["provenance"]["tokens"] == 10
    assert outcome["provenance"]["turns"] == 6
    assert outcome["provenance"]["cost_usd"] == 0.05


# --- every way a confirmation could be produced without a challenge -------------
#
# Each fixture below carries a CONFIRMING payload on purpose. A fixture that
# confirmed nothing would reach `confirmed is False` through the default and
# hold with the branch under test deleted -- which is exactly the defect Task 6
# found in a refused-tool test on this project.


def test_a_stage_that_mutated_the_worktree_cannot_confirm(ctx):
    provider = _FakeProvider(_ok(_payload(confirmed=True), mutation="wrote app.py"))
    outcome, skips = falsify(_CANDIDATE, _reproduction(), ctx, provider)

    assert outcome["confirmed"] is False
    assert outcome["challenged"] is False
    assert any("wrote app.py" in skip for skip in skips), skips


def test_a_stage_refused_a_tool_cannot_confirm(ctx):
    """It answered on less than it asked for. That matters more here than
    anywhere: a falsifier that could not read the file will fail to find the
    counterargument and confirm by default, which is the laundering this stage
    exists to prevent."""
    provider = _FakeProvider(_ok(_payload(confirmed=True), denials=("Read",)))
    outcome, skips = falsify(_CANDIDATE, _reproduction(), ctx, provider)

    assert outcome["confirmed"] is False
    assert outcome["challenged"] is False
    assert any("Read" in skip for skip in skips), skips


def test_a_provider_failure_cannot_confirm(ctx):
    provider = _FakeProvider(
        StageResult(ok=False, data=None, raw="", usage=Usage(), error="CLI missing")
    )
    outcome, skips = falsify(_CANDIDATE, _reproduction(), ctx, provider)

    assert outcome["confirmed"] is False
    assert outcome["challenged"] is False
    assert any("CLI missing" in skip for skip in skips), skips


def test_success_with_no_payload_cannot_confirm(ctx):
    """THE DAY THE GUARANTEE MOVES, simulated rather than asserted away.

    `StageResult.__post_init__` currently forbids `ok=True, data=None`, so this
    state cannot be constructed and the branch guarding it would otherwise be a
    check that quietly never runs -- the exact shape this project keeps getting
    caught by. Forcing it past the frozen dataclass is what makes the guard a
    guard: without it, `result.data.get(...)` raises `AttributeError` out of a
    lens the day that constructor rule is relaxed.
    """
    result = _ok(_payload(confirmed=True))
    object.__setattr__(result, "data", None)
    provider = _FakeProvider(result)
    outcome, skips = falsify(_CANDIDATE, _reproduction(), ctx, provider)

    assert outcome["confirmed"] is False
    assert outcome["challenged"] is False
    assert any("no payload" in skip for skip in skips), skips


def test_a_missing_confirmed_field_is_not_a_confirmation(ctx):
    provider = _FakeProvider(_ok({"strongest_counterargument": "c", "reasoning": "r"}))
    outcome, _ = falsify(_CANDIDATE, _reproduction(), ctx, provider)

    assert outcome["confirmed"] is False
    assert outcome["challenged"] is True


# --- step 2: the kill reasons live in the prompt --------------------------------


@pytest.mark.parametrize(
    "reason",
    [
        "intended behaviour",
        "documentation is stale",
        "test data is wrong",
        "unreachable",
        "feature flag",
        "already fixed",
        "duplicates",
        "too small",
    ],
)
def test_the_prompt_names_the_ways_a_finding_turns_out_to_be_nothing(reason):
    assert reason in load_prompt("falsify")


def test_the_kill_reasons_are_not_an_enum_in_the_schema():
    """As a schema enum they become a menu to pick from instead of a thing to
    think about, and silently exclude the reason nobody listed. The only enum
    the contract carries is the severity scale."""
    schema = load_schema("falsify")
    enums = {
        name: field
        for name, field in schema["properties"].items()
        if "enum" in field
    }
    assert set(enums) == {"severity_adjustment"}


def test_the_prompt_demands_a_counterargument_on_the_confirming_path_too():
    text = load_prompt("falsify")
    assert "required whether you confirm or not" in text.lower()
