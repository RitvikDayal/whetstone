"""The reproduce stage.

TWO KINDS OF TEST, and the split is a consequence of the sandbox decision.

Choosing the container means an artifact can only be EXECUTED where Docker is.
So the verdict mapping -- the part most likely to be got wrong, and the part
that decides whether a defect is real -- is tested as pure logic here, against
reports in pytest's own shape. Those run on all four legs.

The end-to-end execution test needs Docker and is skipped without it. It is the
only one that proves the whole chain: artifact written, container run, report
parsed, worktree left clean. It runs on the Ubuntu CI legs.

Nothing here mocks the executor. Where execution happens it is a real container
running a real command; where it does not, the test is about a refusal.
"""

from __future__ import annotations

import re
import subprocess
import uuid
from pathlib import Path

import pytest

from conftest import (
    build_is_expected as _build_is_expected,
)
from conftest import docker_expected, docker_works, needs_docker
from whetstone._subprocess import ShellResult
from whetstone.lenses.base import RunContext
from whetstone.lenses.code_defects import reproduce as reproduce_module
from whetstone.lenses.code_defects.prompts import load_prompt
from whetstone.lenses.code_defects.reproduce import (
    REPRO_MARKER,
    _bound_failure,
    _sanitise,
    _verdict_from,
    reproduce,
)
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

# The canonical form the prompt teaches: try/except, with a `pytest.fail` whose
# message the controller can attribute on the no-defect path.
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

# WHAT A USER ACTUALLY DECLARES, and it used to carry `-p no:cacheprovider`.
# That flag belongs to the controller, not to the project's own command, and
# having it here meant the container chain test never saw the `.pytest_cache/`
# directory a real `commands.test` leaves in the mounted worktree -- a fixture
# encoding a shape reality never produces. Measured in Task 10: every
# successful reproduction on both fixtures reported "the reproduction modified
# the worktree while it ran" naming four `.pytest_cache` paths, and left the
# directory behind in the repository.
_TEST_COMMAND = "python -m pytest -q"


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
    """A real project with a real defect."""
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


# --- sanitisation ---------------------------------------------------------------


def test_the_reproducer_sees_facts_and_not_opinions():
    """EXACT SET, not a list of `not in` assertions.

    A new hypothesis-shaped key added to the hunt schema later must FAIL this
    test rather than pass it by not being on somebody's deny-list.
    """
    assert set(_sanitise(_CANDIDATE)) == {"subject", "observation", "failure_scenario"}


def test_sanitisation_survives_a_candidate_missing_optional_keys():
    assert _sanitise({"subject": "a.py", "observation": "o"})["failure_scenario"] is None


# --- the verdict mapping, as pure logic -----------------------------------------

_JUNIT = (
    '<?xml version="1.0" encoding="utf-8"?><testsuites><testsuite tests="{n}">'
    "{cases}</testsuite></testsuites>"
)


def _report(tmp_path: Path, *cases: str | None) -> Path:
    """A junit report in pytest's own shape: the assertion message in the
    `message` attribute, the traceback in the element text.

    THE TRACEBACK CONTAINS THE MARKER, and that is the whole point of the
    fixture. pytest's traceback quotes the failing test's own source, and the
    artifact is model-written -- so a real run where the artifact merely PRINTS
    or comments `WHETSTONE-REPRO` produces exactly this shape. A fixture whose
    text never held the marker made the leak-proof test assert the absence of a
    string nothing had written, and an implementation that concatenated the
    text back in would have passed it unchanged.
    """
    body = ""
    for index, message in enumerate(cases):
        if message is None:
            body += f'<testcase name="t{index}"/>'
        else:
            body += (
                f'<testcase name="t{index}"><failure message="{message}">'
                f"def test_reproduces():\n"
                f"    print('{REPRO_MARKER}: nothing to see here')\n"
                f">   assert 1 == 2\n"
                f"</failure></testcase>"
            )
    path = tmp_path / "r.xml"
    path.write_text(_JUNIT.format(n=len(cases), cases=body), encoding="utf-8")
    return path


def test_exit_zero_is_the_defect_reproduced():
    """The artifact asserts the broken behaviour, so passing IS the evidence --
    the opposite of a regression test, deliberately."""
    assert _verdict_from(0, None)[0] == "reproduced"


def test_exit_one_with_the_marker_in_the_single_failure_is_absence(tmp_path):
    bound = _bound_failure(_report(tmp_path, f"Failed: {REPRO_MARKER}: no defect"))
    assert _verdict_from(1, bound)[0] == "absent"


def test_exit_one_without_the_marker_is_a_broken_test(tmp_path):
    """Not absence. Reading any failure as absence is how a tool closes a real
    defect on the strength of its own typo."""
    bound = _bound_failure(_report(tmp_path, "AssertionError: something else"))
    verdict, reason = _verdict_from(1, bound)
    assert verdict == "inconclusive"
    assert "marker" in reason


def test_the_marker_in_the_TRACEBACK_does_not_buy_absence(tmp_path):
    """THE ATTACK the output scan allowed, with a fixture that actually carries it.

    The report's element text holds the marker -- see `_report` -- because that
    is the shape a real run produces when the artifact prints it and then fails
    an unrelated assertion. Only the `message` attribute is read, so the leak
    has somewhere to come from and does not arrive.
    """
    report = _report(tmp_path, "AssertionError: unrelated")
    assert REPRO_MARKER in report.read_text(encoding="utf-8"), (
        "the fixture must contain the marker somewhere, or this test asserts "
        "the absence of a string nothing ever wrote"
    )

    bound = _bound_failure(report)
    assert REPRO_MARKER not in (bound[1] or ""), "the traceback leaked in"
    assert _verdict_from(1, bound)[0] == "inconclusive"


def test_more_than_one_test_cannot_attribute_a_failure(tmp_path):
    bound = _bound_failure(_report(tmp_path, None, f"Failed: {REPRO_MARKER}: x"))
    verdict, reason = _verdict_from(1, bound)
    assert verdict == "inconclusive"
    assert "rather than one" in reason


def test_no_report_at_all_cannot_produce_absence(tmp_path):
    """A declared test command that is not pytest leaves nothing to attribute a
    failure to, and an unattributable failure is not evidence."""
    verdict, reason = _verdict_from(1, _bound_failure(tmp_path / "nothing.xml"))
    assert verdict == "inconclusive"
    assert "no report" in reason


def test_exit_five_is_pytest_collecting_nothing():
    verdict, reason = _verdict_from(5, None)
    assert verdict == "inconclusive"
    assert "no tests" in reason


@pytest.mark.parametrize("code", [2, 3, 4, 137, -1])
def test_any_other_exit_settles_nothing(code):
    assert _verdict_from(code, None)[0] == "inconclusive"


# --- refusals, which need no sandbox --------------------------------------------


def test_without_a_configured_sandbox_nothing_is_executed(ctx):
    """THE CRITICAL, closed.

    `kind: "pytest"` bounds what INVOKES the artifact and nothing whatever
    about what it can do -- a pytest file is an arbitrary Python program. No
    container, no execution, and the finding caps at grade B.
    """
    provider = _FakeProvider(
        _ok(_payload(artifact={"kind": "pytest", "content": _PASSES}))
    )
    result, skips = reproduce(_CANDIDATE, ctx, provider, _TEST_COMMAND)

    assert result["verdict"] == "not executed"
    assert result["reproduced"] is False
    assert any("sandbox image is configured" in skip for skip in skips)


def test_a_refusal_writes_nothing_into_the_project(ctx):
    """Checked BEFORE the artifact is written, so a refusal leaves no file
    behind for the user to wonder about."""
    provider = _FakeProvider(
        _ok(_payload(artifact={"kind": "pytest", "content": _PASSES}))
    )
    reproduce(_CANDIDATE, ctx, provider, _TEST_COMMAND)

    assert [p.name for p in ctx.project_root.rglob("*whetstone_repro*")] == []
    assert list(ctx.project_root.rglob("*.xml")) == []


@pytest.mark.parametrize("kind", ["script", "command"])
def test_only_pytest_artifacts_are_considered(kind, ctx):
    provider = _FakeProvider(
        _ok(_payload(artifact={"kind": kind, "content": "rm -rf /"}))
    )
    result, skips = reproduce(_CANDIDATE, ctx, provider, _TEST_COMMAND, "an-image")

    assert result["has_runnable_artifact"] is False
    assert any(kind in skip for skip in skips)


def test_a_null_artifact_is_an_honest_answer_not_a_failure(ctx):
    """The prompt tells the model to say so rather than fabricate. That has to
    be graded as honesty, which means it cannot also be a skip."""
    provider = _FakeProvider(_ok(_payload(artifact=None, reproduced=False)))
    result, skips = reproduce(_CANDIDATE, ctx, provider, _TEST_COMMAND, "an-image")

    assert result["has_runnable_artifact"] is False
    assert result["verdict"] == "not attempted"
    assert skips == []


def test_a_provider_failure_is_a_skip(ctx):
    provider = _FakeProvider(
        StageResult(ok=False, data=None, raw="", usage=Usage(), error="CLI missing")
    )
    result, skips = reproduce(_CANDIDATE, ctx, provider, _TEST_COMMAND, "an-image")

    # NOT `inconclusive`. That word means a container ran and settled nothing,
    # and the provider never returned an artifact for one to run.
    assert result["verdict"] == "not attempted"
    assert result["executed"] is False
    assert any("CLI missing" in skip for skip in skips)


@pytest.mark.parametrize(
    ("result", "image", "verdict"),
    [
        (
            _ok(
                _payload(artifact={"kind": "pytest", "content": _PASSES}),
                mutation="app.py changed",
            ),
            "an-image",
            "not executed",
        ),
        (
            StageResult(
                ok=False, data=None, raw="", usage=Usage(), error="CLI missing"
            ),
            "an-image",
            "not attempted",
        ),
        (_ok(_payload(artifact=None)), "an-image", "not attempted"),
        (
            _ok(_payload(artifact={"kind": "script", "content": "rm -rf /"})),
            "an-image",
            "not executed",
        ),
        (
            _ok(_payload(artifact={"kind": "pytest", "content": "   \n"})),
            "an-image",
            "not executed",
        ),
        (
            _ok(_payload(artifact={"kind": "pytest", "content": _PASSES})),
            None,
            "not executed",
        ),
    ],
    ids=[
        "mutation",
        "provider-failed",
        "no-artifact",
        "wrong-kind",
        "empty",
        "no-sandbox",
    ],
)
def test_every_path_that_starts_no_container_records_that_nothing_ran(
    result, image, verdict, ctx
):
    """`executed` is a FACT, and `inconclusive` is a post-execution word.

    Each of these returns before a container starts. Each of them used to leave
    the verdict at its `inconclusive` default -- which legitimately means "it
    ran and settled nothing" -- so every consumer reading execution out of that
    word was told a run had happened. Both halves are asserted, and the verdict
    exactly rather than as a set: a branch that stopped distinguishing "nothing
    was written" from "it was written and refused" would otherwise pass.
    """
    outcome, _ = reproduce(_CANDIDATE, ctx, _FakeProvider(result), _TEST_COMMAND, image)

    assert outcome["executed"] is False
    assert outcome["verdict"] == verdict
    assert outcome["reproduced"] is False


# --- what gets sent -------------------------------------------------------------


def test_the_stage_runs_under_the_reproduce_profile_with_no_shell(ctx):
    provider = _FakeProvider(_ok(_payload(artifact=None)))
    reproduce(_CANDIDATE, ctx, provider, _TEST_COMMAND, "an-image")

    request = provider.requests[0]
    assert request.stage == "reproduce"
    assert "Bash" not in request.permissions.available_tools


def test_the_prompt_carries_only_the_sanitised_fields(ctx):
    """The anchoring guard, asserted at the boundary that actually sends it."""
    provider = _FakeProvider(_ok(_payload(artifact=None)))
    reproduce(_CANDIDATE, ctx, provider, _TEST_COMMAND, "an-image")

    sent = provider.requests[0].prompt
    assert _CANDIDATE["observation"] in sent
    assert _CANDIDATE["root_cause_hypothesis"] not in sent
    assert _CANDIDATE["title"] not in sent
    assert "0.8" not in sent


def test_the_prompt_TEMPLATE_cannot_ask_for_anything_sanitisation_withholds():
    """The guard that matters is on the markdown, not on the code.

    `_prompt_for` names its three substitutions explicitly, so passing it the
    raw candidate changes nothing -- a mutation proved that. The reachable leak
    is adding `$root_cause_hypothesis` to the prompt FILE.
    """
    placeholders = set(re.findall(r"\$(\w+)", load_prompt("reproduce")))
    assert placeholders <= set(_sanitise(_CANDIDATE)), sorted(placeholders)


def test_the_prompt_teaches_the_form_that_can_reach_absence():
    """`with pytest.raises(...)` fails with pytest's own DID NOT RAISE message
    against a fixed implementation. That carries no marker, so absence would be
    unreachable through the very form the prompt recommends."""
    template = load_prompt("reproduce")
    assert "pytest.fail(" in template
    assert REPRO_MARKER in template
    assert "Do not use `with pytest.raises(...)` for this." in template


# --- the whole chain, which needs a container ------------------------------------


@needs_docker
@pytest.mark.parametrize(
    "declared",
    [_TEST_COMMAND, f"{_TEST_COMMAND} --lf"],
    ids=["plain", "cache-dependent"],
)
def test_the_controller_runs_the_artifact_inside_a_container(declared, ctx, tmp_path):
    """THE ONE THAT PROVES THE CHAIN: artifact written, container run, report
    parsed, verdict taken from the exit code, worktree left clean.

    The image needs pytest, and the container has no network, so it is built
    here rather than installed at run time. Slow, and the only way to show the
    whole path works.

    THE SECOND CASE IS A TARGET WHOSE OWN COMMAND NEEDS THE CACHE. `--lf`,
    `--ff` and `--nf` are options the cache plugin registers, and the `cache`
    fixture is the plugin too. The first fix for the `.pytest_cache` mutation
    appended `-p no:cacheprovider`, which stopped the directory appearing by
    DELETING THE FEATURE: pytest then rejects `--lf` during startup, exits 4,
    and the controller files an `inconclusive` verdict about the user's defect
    on the strength of its own flag. That is the tooling answering a question
    about the code -- the exact class of wrong answer the marker convention
    exists to prevent -- so the isolation has to be a cache DIRECTORY the
    sandbox owns, not a disabled plugin.
    """
    context = tmp_path / "img"
    context.mkdir()
    (context / "Dockerfile").write_text(
        "FROM python:3.11-slim\nRUN pip install --no-cache-dir pytest\n",
        encoding="utf-8",
    )
    # A unique tag: a shared runner can have two of these in flight, and a
    # fixed one makes them clobber each other's image.
    tag = f"whetstone-test-sandbox:{uuid.uuid4().hex[:12]}"
    built = subprocess.run(
        ["docker", "build", "-q", "-t", tag, str(context)],
        capture_output=True,
        timeout=900,
    )
    if built.returncode != 0:
        detail = built.stderr.decode("utf-8", "replace")[-300:]
        # A FAILED BUILD IS NOT A SKIP where the build is expected to work.
        # `docker_works()` only says the daemon answered, so a registry outage,
        # a proxy or a rate limit would otherwise skip the only test that
        # proves the chain -- and the Linux leg would still report success.
        if _build_is_expected():
            pytest.fail(f"the sandbox image failed to build on a Linux CI leg: {detail}")
        pytest.skip(f"could not build the test image: {detail!r}")

    provider = _FakeProvider(
        _ok(_payload(artifact={"kind": "pytest", "content": _PASSES}))
    )
    result, skips = reproduce(_CANDIDATE, ctx, provider, declared, tag)

    assert result["verdict"] == "reproduced", skips
    assert result["reproduced"] is True
    assert [p.name for p in ctx.project_root.rglob("*whetstone_repro*")] == []
    assert list(ctx.project_root.rglob("*.xml")) == []
    # AND THE WORKTREE IS LEFT AS IT WAS FOUND. pytest's cache plugin writes
    # `.pytest_cache/` into the mount, which is neither the artifact nor
    # anything the model wrote -- so it was attributed to the reproduction as a
    # worktree mutation on every successful run, and it stayed in the user's
    # repository afterwards. A skip that fires every time is a skip nobody
    # reads.
    #
    # ASSERTED ON THE WORKTREE, not on the argv. `-o cache_dir=...` appearing
    # in the command proves a flag was passed; only this proves no cache landed
    # in somebody's repository, and this project has shipped that exact
    # confusion before.
    assert list(ctx.project_root.rglob(".pytest_cache")) == []
    assert result["mutation"] is None, skips
    assert skips == []


@docker_expected
def test_docker_is_available_where_it_is_expected():
    """Without Docker the chain test above disappears and the run still reports
    success. Gated on Linux CI rather than asserted everywhere, because Docker
    is genuinely optional on a laptop and the Windows runners default to
    Windows containers."""
    assert docker_works(), (
        "docker is unavailable on a Linux CI leg, so the reproduce chain is "
        "unverified"
    )


def _stub_container(monkeypatch, *, returncode: int = 0, timed_out: bool = False):
    """A container that ran, without needing one.

    The chain test above proves a real container; these prove what the
    controller DOES with each outcome, and they have to run on every leg. Both
    the availability probe and the run are replaced, because the probe would
    otherwise refuse before any of this is reached on a machine with no Docker
    -- which is exactly how these branches stayed unasserted.
    """
    monkeypatch.setattr(reproduce_module, "availability", lambda _image: None)
    monkeypatch.setattr(
        reproduce_module,
        "run_sandboxed",
        lambda *a, **k: ShellResult(
            returncode=returncode, stdout="", stderr="", timed_out=timed_out
        ),
    )


@pytest.mark.parametrize(
    ("returncode", "timed_out", "executed", "verdict", "reproduced"),
    [
        (0, False, True, "reproduced", True),
        (-1, True, True, "inconclusive", False),
        (125, False, False, "not executed", False),
        (1, False, True, "inconclusive", False),
        (5, False, True, "inconclusive", False),
        (2, False, True, "inconclusive", False),
    ],
    ids=["exit-0", "timeout", "docker-125", "exit-1-no-report", "exit-5", "exit-2"],
)
def test_what_the_controller_records_for_each_container_outcome(
    returncode, timed_out, executed, verdict, reproduced, ctx, monkeypatch
):
    """THE THREE POST-CONTAINER BRANCHES, none of which a Docker-gated test
    reaches on a leg without Docker.

    They are also the only place `executed` and the verdict can legitimately
    disagree, which is the only place a regression re-deriving execution from
    the verdict word is catchable at all:

    - a TIMEOUT ran. It was killed part way through, so `inconclusive` is
      earned and `executed` is True.
    - docker exit 125 is docker refusing to start a container. Nothing was
      executed, and reading `inconclusive` off that exit code -- which is what
      `_verdict_from` would return for it -- would file a finding as evidence
      of a run that never happened.
    - every other code came out of the container, so it ran whatever it
      concluded.
    """
    _stub_container(monkeypatch, returncode=returncode, timed_out=timed_out)
    provider = _FakeProvider(
        _ok(_payload(artifact={"kind": "pytest", "content": _PASSES}))
    )

    outcome, _ = reproduce(_CANDIDATE, ctx, provider, _TEST_COMMAND, "an-image")

    assert outcome["executed"] is executed
    assert outcome["verdict"] == verdict
    assert outcome["reproduced"] is reproduced
    # It got far enough to write one, on every one of these paths.
    assert outcome["has_runnable_artifact"] is True


def test_a_container_docker_never_started_says_so_rather_than_settling_nothing(
    ctx, monkeypatch
):
    """The skip, not just the verdict. `_verdict_from(125, ...)` says the run
    'settles nothing', which reads as a container that ran and was
    unhelpful; the user needs to know no container started -- a bad image name
    is the ordinary cause and it is fixable."""
    _stub_container(monkeypatch, returncode=125)
    provider = _FakeProvider(
        _ok(_payload(artifact={"kind": "pytest", "content": _PASSES}))
    )

    _, skips = reproduce(_CANDIDATE, ctx, provider, _TEST_COMMAND, "an-image")

    assert any("without starting a container" in skip for skip in skips), skips
    assert not any("settles nothing" in skip for skip in skips), skips


def test_a_timed_out_container_is_recorded_as_having_run(ctx, monkeypatch):
    """Named separately from the table because it is the one outcome where
    `executed` is True and the verdict is `inconclusive` -- the pair that makes
    the word legal. A fix that set `executed` from the exit code alone would
    get this one wrong and the table would still be green on five of six."""
    _stub_container(monkeypatch, returncode=-1, timed_out=True)
    provider = _FakeProvider(
        _ok(_payload(artifact={"kind": "pytest", "content": _PASSES}))
    )

    outcome, skips = reproduce(_CANDIDATE, ctx, provider, _TEST_COMMAND, "an-image")

    assert outcome["executed"] is True
    assert outcome["verdict"] == "inconclusive"
    assert any("did not finish within" in skip for skip in skips), skips


def test_the_bytecode_setting_reaches_the_container_as_an_env_var(ctx, monkeypatch):
    """Not as a `VAR=value` command prefix. A prefix binds to one simple
    command, so a compound `test_command` -- `cd sub && python -m pytest` --
    would apply it to `cd` alone and pytest would leave `__pycache__` in the
    mounted worktree: a file Whetstone put in the user's repository and did not
    remove, which the sentinel then reports as a mutation Whetstone caused."""
    captured: dict = {}

    def fake_run(command, worktree, image, timeout, env=None):
        captured["command"] = command
        captured["env"] = env
        return ShellResult(
            returncode=0, stdout="", stderr="", timed_out=False
        )

    monkeypatch.setattr(reproduce_module, "availability", lambda _image: None)
    monkeypatch.setattr(reproduce_module, "run_sandboxed", fake_run)

    provider = _FakeProvider(
        _ok(_payload(artifact={"kind": "pytest", "content": _PASSES}))
    )
    reproduce(_CANDIDATE, ctx, provider, _TEST_COMMAND, "an-image")

    assert captured["env"] == {"PYTHONDONTWRITEBYTECODE": "1"}
    assert "PYTHONDONTWRITEBYTECODE" not in captured["command"]
