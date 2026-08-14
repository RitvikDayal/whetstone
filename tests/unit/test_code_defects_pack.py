"""The code-defects lens pack: the thing that makes the four stages a pipeline.

WHAT IS REAL HERE AND WHAT IS NOT. `hunt` and `falsify` are real in every test
below -- they are driven by a fake `Provider` returning whole `StageResult`s, so
their judgement of `denials`, `mutation` and `turns` executes for real.
`reproduce` is real too on the default path, where it asks the provider for an
artifact and then REFUSES to execute it because no sandbox image is configured;
that is the fail-closed path and it needs no Docker. Only the tests that need a
verdict of `reproduced` -- which requires a container -- replace `reproduce`, and
they are the ones asserting how a verdict is wired into the grade rather than
how a verdict is produced.

EVERY FAKE SPENDS REAL MONEY. A budget test whose provider returns `Usage()`
proves nothing at all: nothing was spent, so nothing could exhaust. The usage
below carries the measured cache shape.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from whetstone.budget import Budget
from whetstone.config.model import (
    BudgetConfig,
    CeilingConfig,
    CommandsConfig,
    EnvironmentConfig,
    ModelConfig,
    ProjectConfig,
    WhetstoneConfig,
)
from whetstone.lenses.base import Candidate, LensPack, RunContext, Severity
from whetstone.lenses.code_defects import pack as pack_module
from whetstone.lenses.code_defects.grade import Grade
from whetstone.lenses.code_defects.pack import CodeDefectsPack
from whetstone.lenses.registry import get_lens
from whetstone.provider.base import StageRequest, StageResult, Usage

_MEASURED = Usage(
    input_tokens=4,
    output_tokens=120,
    cache_creation_input_tokens=41036,
    cache_read_input_tokens=900,
    cost_usd=0.0921,
    wall_seconds=11.3,
    source="usage",
)


def _finding(**overrides) -> dict:
    base = {
        "subject": "app.py:12",
        "title": "add() raises on empty input",
        "observation": "add() indexes values[0] with no length check.",
        "root_cause_hypothesis": "The caller does not guard against an empty list.",
        "alternative_explanations": ["Callers may guarantee a non-empty list."],
        "failure_scenario": "add([]) raises IndexError.",
        "severity": "high",
        "confidence": 0.8,
    }
    base.update(overrides)
    return base


_ARTIFACT = {
    "kind": "pytest",
    "path": "test_repro.py",
    "content": (
        "import pytest\n"
        "from app import add\n\n"
        "def test_reproduces():\n"
        "    try:\n"
        "        add([])\n"
        "    except IndexError:\n"
        "        return\n"
        "    pytest.fail('WHETSTONE-REPRO: add([]) returned instead of raising')\n"
    ),
}


def _repro_payload(**overrides) -> dict:
    base = {
        "reproduced": True,
        "steps": ["call add([])"],
        "expected": "returns 0",
        "actual": "raises IndexError",
        "artifact": _ARTIFACT,
        "environment": None,
        "notes": None,
    }
    base.update(overrides)
    return base


def _falsify_payload(**overrides) -> dict:
    base = {
        "confirmed": True,
        "strongest_counterargument": "Callers might always pass a non-empty list.",
        "reasoning": "Two call sites pass user input straight through.",
        "remaining_uncertainty": [],
        "severity_adjustment": None,
        "notes": None,
    }
    base.update(overrides)
    return base


class _FakeProvider:
    """Serves queued results per stage and records every request."""

    name = "fake"

    def __init__(self, **queues: list[StageResult]) -> None:
        self._queues = {stage: list(results) for stage, results in queues.items()}
        self.requests: list[StageRequest] = []

    def run_stage(self, request: StageRequest) -> StageResult:
        self.requests.append(request)
        queue = self._queues.get(request.stage)
        if not queue:
            raise AssertionError(
                f"stage {request.stage!r} asked for more runs than were queued"
            )
        return queue.pop(0)

    def stages(self) -> list[str]:
        return [request.stage for request in self.requests]


def _ok(data: dict, *, usage: Usage = _MEASURED, turns: int = 4) -> StageResult:
    return StageResult(
        ok=True, data=data, raw="{}", usage=usage, error=None, turns=turns
    )


def _provider(
    *,
    findings: list[dict] | None = None,
    repro: dict | None = None,
    falsified: list[dict] | None = None,
    hunts: int = 1,
) -> _FakeProvider:
    hunt_payload = {"findings": findings or [_finding()], "notes": None}
    return _FakeProvider(
        hunt=[_ok(hunt_payload) for _ in range(hunts)],
        reproduce=[_ok(repro or _repro_payload()) for _ in range(4)],
        falsify=[_ok(p) for p in (falsified or [_falsify_payload()] * 4)],
    )


@pytest.fixture
def ctx(tmp_path):
    lines = ['"""A small module with a real defect."""', ""]
    lines += [f"CONST_{n} = {n}" for n in range(1, 10)]
    lines += ["", "def add(values):", "    return values[0]", ""]
    (tmp_path / "app.py").write_text("\n".join(lines), encoding="utf-8")
    (tmp_path / "other.py").write_text("\n".join(lines), encoding="utf-8")
    state = tmp_path / ".whetstone"
    state.mkdir()
    return RunContext(
        project_root=tmp_path,
        state_root=state,
        files=(Path("app.py"), Path("other.py")),
        tier="deep",
        lens_options={"options": {"angles": ["error handling"]}},
        run_id="run-abc123",
    )


def _pack(provider, **overrides) -> CodeDefectsPack:
    base = dict(
        provider=provider,
        test_command="python -m pytest -q",
        ceiling_usd=100.0,
    )
    base.update(overrides)
    return CodeDefectsPack(**base)


def _run(pack, ctx) -> list[Candidate]:
    return list(pack.run(ctx))


# --- the pack is a pack ---------------------------------------------------------


def test_it_satisfies_the_lens_pack_protocol():
    assert isinstance(CodeDefectsPack(), LensPack)
    assert CodeDefectsPack().name == "code-defects"
    assert CodeDefectsPack().max_autonomy == 3


def test_it_is_registered_so_a_run_can_reach_it():
    assert get_lens("code-defects") is not None


# --- tier gating ----------------------------------------------------------------


def test_quick_does_not_support_this_lens():
    pack = CodeDefectsPack()
    assert pack.supports_tier("quick") is False
    assert pack.supports_tier("standard") is True
    assert pack.supports_tier("deep") is True


def test_running_at_quick_declines_and_says_it_costs_money(ctx):
    ctx.tier = "quick"
    provider = _provider()
    assert _run(_pack(provider), ctx) == []
    assert provider.requests == [], "quick must not spend anything"
    assert any("quick" in skip and "cost" in skip.lower() for skip in ctx.skips)


def test_standard_carries_only_high_severity_candidates(ctx):
    ctx.tier = "standard"
    provider = _provider(
        findings=[
            _finding(severity="medium", subject="app.py:12", title="a medium one"),
            _finding(severity="high", subject="other.py:12", title="a high one"),
        ]
    )
    found = _run(_pack(provider), ctx)

    assert [c.subject for c in found] == ["other.py:12"]
    assert any(
        "a medium one" in skip and "standard" in skip for skip in ctx.skips
    ), ctx.skips


def test_deep_carries_everything(ctx):
    provider = _provider(
        findings=[
            _finding(severity="low", subject="app.py:12", title="a low one"),
            _finding(severity="high", subject="other.py:12", title="a high one"),
        ]
    )
    found = _run(_pack(provider), ctx)

    assert sorted(c.subject for c in found) == ["app.py:12", "other.py:12"]


# --- nothing to look at ---------------------------------------------------------


def test_no_files_in_scope_declines_without_spending(ctx):
    ctx.files = ()
    provider = _provider()

    assert _run(_pack(provider), ctx) == []
    assert provider.requests == []
    assert any("no files" in skip.lower() for skip in ctx.skips)


# --- the grade wiring -----------------------------------------------------------


def test_a_surviving_finding_is_graded_and_carries_its_reason(ctx, monkeypatch):
    _stub_reproduction(monkeypatch, verdict="reproduced", reproduced=True)
    found = _run(_pack(_provider()), ctx)

    assert len(found) == 1
    candidate = found[0]
    assert candidate.lens == "code-defects"
    assert candidate.severity is Severity.high
    data = json.loads(candidate.evidence.to_json())["data"]
    assert data["grade"] == Grade.A
    assert "reproduced" in data["grade_reason"]


def test_reproduced_comes_from_the_controller_not_from_the_payload(ctx, monkeypatch):
    """The model claimed it reproduced; the controller's own run said otherwise."""
    _stub_reproduction(
        monkeypatch,
        verdict="inconclusive",
        reproduced=False,
        payload=_repro_payload(reproduced=True),
    )
    found = _run(_pack(_provider()), ctx)

    data = json.loads(found[0].evidence.to_json())["data"]
    assert data["grade"] == Grade.C


def test_no_runnable_artifact_caps_the_grade(ctx, monkeypatch):
    _stub_reproduction(
        monkeypatch, verdict="reproduced", reproduced=True, runnable=False
    )
    found = _run(_pack(_provider()), ctx)

    data = json.loads(found[0].evidence.to_json())["data"]
    assert data["grade"] == Grade.B


def test_the_falsifier_killing_it_grades_d(ctx, monkeypatch):
    _stub_reproduction(monkeypatch, verdict="reproduced", reproduced=True)
    provider = _provider(falsified=[_falsify_payload(confirmed=False)])
    found = _run(_pack(provider), ctx)

    data = json.loads(found[0].evidence.to_json())["data"]
    assert data["grade"] == Grade.D
    assert data["falsify"]["challenged"] is True


def test_a_finding_that_was_never_challenged_is_not_graded_as_killed(ctx, monkeypatch):
    """Task 8 added `challenged` for exactly this. A falsify stage that mutated
    the worktree has its verdict discarded -- `confirmed` is False because
    nothing challenged the finding, not because something killed it, and
    grading it D would report a defect as dismissed by a stage that never ran."""
    _stub_reproduction(monkeypatch, verdict="reproduced", reproduced=True)
    provider = _FakeProvider(
        hunt=[_ok({"findings": [_finding()], "notes": None})],
        falsify=[
            StageResult(
                ok=True,
                data=_falsify_payload(),
                raw="{}",
                usage=_MEASURED,
                error=None,
                turns=3,
                mutation="app.py was modified",
            )
        ],
    )
    found = _run(_pack(provider), ctx)

    assert found == []
    assert any(
        "app.py:12" in skip and "challenge" in skip.lower() for skip in ctx.skips
    ), ctx.skips


def test_alternative_explanations_are_counted_from_the_hunt_field(ctx, monkeypatch):
    """Counted, not just copied. `grade_finding` says so in its reason when the
    hunter offered none, so asserting the recorded list alone would pass against
    a pack that hands the grader a hardcoded zero."""
    _stub_reproduction(monkeypatch, verdict="reproduced", reproduced=True)
    provider = _provider(findings=[_finding(alternative_explanations=["a", "b", "c"])])
    found = _run(_pack(provider), ctx)

    data = json.loads(found[0].evidence.to_json())["data"]
    assert data["alternative_explanations"] == ["a", "b", "c"]
    assert "no alternative explanation" not in data["grade_reason"]


def test_a_hunter_that_offered_no_alternative_is_said_to_have_offered_none(
    ctx, monkeypatch
):
    """The other half of the count. Without this the pack could pass a constant."""
    _stub_reproduction(monkeypatch, verdict="reproduced", reproduced=True)
    provider = _provider(findings=[_finding(alternative_explanations=[])])
    found = _run(_pack(provider), ctx)

    data = json.loads(found[0].evidence.to_json())["data"]
    assert "no alternative explanation" in data["grade_reason"]


@pytest.mark.parametrize("confidence", [0.0, 0.5, 1.0])
def test_model_confidence_is_recorded_and_never_changes_the_grade(
    ctx, monkeypatch, confidence
):
    _stub_reproduction(monkeypatch, verdict="reproduced", reproduced=True)
    provider = _provider(findings=[_finding(confidence=confidence)])
    found = _run(_pack(provider), ctx)

    data = json.loads(found[0].evidence.to_json())["data"]
    assert data["model_confidence"] == confidence
    assert data["grade"] == Grade.A


def test_an_unmappable_severity_discards_the_candidate_with_a_reason(ctx):
    provider = _provider(findings=[_finding(severity="catastrophic")])

    assert _run(_pack(provider), ctx) == []
    assert any("catastrophic" in skip for skip in ctx.skips), ctx.skips


# --- the budget stops the run ---------------------------------------------------


def test_hitting_the_ceiling_names_every_candidate_it_did_not_reach(ctx, monkeypatch):
    """One stage costs 0.0921, so a 0.05 ceiling is spent by the first hunt."""
    _stub_reproduction(monkeypatch, verdict="reproduced", reproduced=True)
    provider = _provider(
        findings=[
            _finding(subject="app.py:12", title="the first one"),
            _finding(subject="other.py:12", title="the second one"),
        ]
    )
    found = _run(_pack(provider, ceiling_usd=0.05), ctx)

    assert found == []
    stopped = [skip for skip in ctx.skips if "budget" in skip.lower()]
    assert stopped, ctx.skips
    assert any("the first one" in skip for skip in stopped)
    assert any("the second one" in skip for skip in stopped)


def test_the_ceiling_refuses_later_stages_through_the_provider(ctx):
    """The hunt runs one stage per angle inside a single call, so a ceiling
    checked only between candidates could never stop it."""
    ctx.lens_options["options"]["angles"] = ["error handling", "boundaries"]
    provider = _provider(hunts=2)
    _run(_pack(provider, ceiling_usd=0.05), ctx)

    assert provider.stages().count("hunt") == 1, "the second angle must be refused"
    assert any("budget" in skip.lower() for skip in ctx.skips), ctx.skips


def test_without_a_ceiling_the_run_is_unbounded_and_says_so(ctx, monkeypatch):
    _stub_reproduction(monkeypatch, verdict="reproduced", reproduced=True)
    found = _run(_pack(_provider(), ceiling_usd=None), ctx)

    assert len(found) == 1
    assert any("ceiling" in skip.lower() for skip in ctx.skips), ctx.skips


def test_a_daily_call_ceiling_is_reported_as_not_enforced(ctx, monkeypatch):
    _stub_reproduction(monkeypatch, verdict="reproduced", reproduced=True)
    _run(_pack(_provider(), calls_per_day=10), ctx)

    assert any("calls_per_day" in skip for skip in ctx.skips), ctx.skips


# --- the cost record ------------------------------------------------------------


def test_every_stage_cost_is_recorded_against_the_run(ctx):
    """The estimator is fit to this after Task 10. The predecessor's estimates
    were 4-17x low because only a total survived."""
    _run(_pack(_provider()), ctx)

    record = ctx.state_root / "costs" / "run-abc123.json"
    assert record.exists()
    written = json.loads(record.read_text(encoding="utf-8"))
    assert written["run_id"] == "run-abc123"
    assert [entry["stage"] for entry in written["stages"]] == [
        "hunt",
        "reproduce",
        "falsify",
    ]
    assert written["stages"][0]["cost_usd"] == pytest.approx(0.0921)
    # total_tokens, never input_tokens: 4 against 41,036 on the same call.
    assert written["stages"][0]["tokens"] == 42060
    assert written["spent_usd"] == pytest.approx(0.2763)


def test_the_cost_record_survives_a_run_that_found_nothing(ctx):
    provider = _FakeProvider(
        hunt=[_ok({"findings": [], "notes": "read app.py, nothing here"})]
    )
    _run(_pack(provider), ctx)

    record = ctx.state_root / "costs" / "run-abc123.json"
    assert record.exists()
    assert json.loads(record.read_text(encoding="utf-8"))["spent_usd"] > 0


def test_an_empty_hunt_reports_its_own_reason(ctx):
    provider = _FakeProvider(
        hunt=[_ok({"findings": [], "notes": "read app.py, nothing here"})]
    )
    _run(_pack(provider), ctx)

    assert any("nothing here" in skip for skip in ctx.skips), ctx.skips


# --- the real reproduce path, with no container -----------------------------


def test_with_no_sandbox_the_evidence_is_never_executed_and_the_grade_shows_it(ctx):
    """Not a stub. `reproduce` runs for real, asks the provider for an artifact,
    and refuses to execute it because no image is configured -- so nothing was
    reproduced and the finding cannot be graded above C."""
    found = _run(_pack(_provider()), ctx)

    assert len(found) == 1
    data = json.loads(found[0].evidence.to_json())["data"]
    assert data["reproduction"]["verdict"] == "not executed"
    assert data["grade"] == Grade.C
    assert any("sandbox" in skip.lower() for skip in ctx.skips), ctx.skips


def test_without_a_declared_test_command_nothing_is_reproduced(ctx):
    """No `environment.commands.test` means there is nothing to execute the
    artifact with, so the reproduce stage is not paid for at all."""
    provider = _provider()
    found = _run(_pack(provider, test_command=None), ctx)

    assert "reproduce" not in provider.stages()
    assert len(found) == 1
    data = json.loads(found[0].evidence.to_json())["data"]
    assert data["grade"] == Grade.C
    # Asserted directly, because the grade cannot see it: `reproduced=False`
    # decides C before `has_runnable_artifact` is ever consulted, so a stub
    # claiming a runnable artifact nobody could run would grade identically.
    assert data["reproduction"]["has_runnable_artifact"] is False
    assert data["reproduction"]["verdict"] == "not attempted"
    assert any("commands.test" in skip for skip in ctx.skips), ctx.skips


def test_a_refused_hunt_stage_reports_its_reason_to_the_user(ctx):
    """The hunt judges its own stage -- a refused tool discards its findings --
    and the reason has to travel out through the pack. `ctx.skip` is the only
    channel a lens has, and dropping the list loses the whole event."""
    provider = _FakeProvider(
        hunt=[
            StageResult(
                ok=True,
                data={"findings": [_finding()], "notes": None},
                raw="{}",
                usage=_MEASURED,
                error=None,
                turns=3,
                denials=("Write",),
            )
        ]
    )
    found = _run(_pack(provider), ctx)

    assert found == []
    assert any("Write" in skip for skip in ctx.skips), ctx.skips


# --- configuration --------------------------------------------------------------


def test_configure_takes_what_it_needs_from_the_config():
    cfg = WhetstoneConfig(
        project=ProjectConfig(name="p"),
        environment=EnvironmentConfig(commands=CommandsConfig(test="pytest -q")),
        model=ModelConfig(provider="claude-cli"),
        budget=BudgetConfig(ceiling=CeilingConfig(usd_per_run=2.5, calls_per_day=7)),
    )
    configured = CodeDefectsPack().configure(cfg)

    assert configured is not None
    assert configured.test_command == "pytest -q"
    assert configured.ceiling_usd == 2.5
    assert configured.calls_per_day == 7
    assert configured.provider_name == "claude-cli"


def test_configure_returns_a_new_pack_rather_than_editing_the_registered_one():
    """The registry hands out one instance for the life of the process, so
    configuring in place would leak one project's test command into the next."""
    registered = CodeDefectsPack()
    cfg = WhetstoneConfig(
        project=ProjectConfig(name="p"),
        environment=EnvironmentConfig(commands=CommandsConfig(test="pytest -q")),
    )
    configured = registered.configure(cfg)

    assert configured is not registered
    assert registered.test_command is None


def test_a_missing_provider_is_a_skip_rather_than_a_crash(ctx):
    pack = CodeDefectsPack(provider_name="nosuchprovider", test_command="pytest")

    assert _run(pack, ctx) == []
    assert any("nosuchprovider" in skip for skip in ctx.skips), ctx.skips


# --- helpers --------------------------------------------------------------------


def _stub_reproduction(
    monkeypatch,
    *,
    verdict: str,
    reproduced: bool,
    runnable: bool = True,
    payload: dict | None = None,
):
    """Replace the reproduce stage with a fixed outcome.

    Only for the tests about how a verdict reaches the grade. Producing a
    verdict of `reproduced` needs a container, and what those tests are about is
    the wiring, not the execution -- `test_reproduce.py` owns that and runs a
    real container to prove it.
    """

    def _fake(candidate, ctx, provider, test_command, sandbox_image=None):
        return (
            {
                "reproduced": reproduced,
                "verdict": verdict,
                "has_runnable_artifact": runnable,
                "mutation": None,
                "payload": payload or _repro_payload(),
                "provenance": {"turns": 3, "cost_usd": 0.0487, "tokens": 13700},
            },
            [],
        )

    monkeypatch.setattr(pack_module, "reproduce", _fake)


def test_the_budget_is_not_shared_between_runs(ctx, monkeypatch):
    """A Budget held on the pack would carry one project's spend into the next
    run through the registry's single instance. One run here spends 0.1842, so
    a shared budget would find the 0.20 ceiling already gone on the second."""
    _stub_reproduction(monkeypatch, verdict="reproduced", reproduced=True)
    pack = _pack(_provider(hunts=2), ceiling_usd=0.20)

    assert len(_run(pack, ctx)) == 1
    assert len(_run(pack, ctx)) == 1, "the second run started with a fresh budget"


def test_a_budget_object_is_not_kept_on_the_pack():
    assert not any(
        isinstance(value, Budget) for value in vars(CodeDefectsPack()).values()
    )
