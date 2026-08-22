"""The hunt stage.

Nothing here mocks the provider's internals. A fake `Provider` returns whole
`StageResult`s -- the same object the real one returns -- so the stage's
handling of `denials`, `mutation` and `turns` executes for real. Those three
fields exist because the provider deliberately does not judge them: invariant 2
says the deterministic layer decides, and this is that layer.
"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from whetstone.lenses.base import RunContext
from whetstone.lenses.code_defects.hunt import hunt
from whetstone.lenses.code_defects.prompts import load_prompt
from whetstone.provider.base import StageRequest, StageResult, Usage

_FINDING = {
    "subject": "app.py:12",
    "title": "add() raises on empty input",
    "observation": "add() indexes values[0] with no length check.",
    "root_cause_hypothesis": "The caller does not guard against an empty list.",
    "alternative_explanations": ["Callers may guarantee a non-empty list."],
    "failure_scenario": "add([]) raises IndexError.",
    "severity": "high",
    "confidence": 0.8,
}


class _FakeProvider:
    """Returns queued results and records every request it was handed."""

    name = "fake"

    def __init__(self, *results: StageResult) -> None:
        self._results = list(results)
        self.requests: list[StageRequest] = []

    def run_stage(self, request: StageRequest) -> StageResult:
        self.requests.append(request)
        if not self._results:
            raise AssertionError("the stage asked for more runs than were queued")
        return self._results.pop(0)


def _ok(data: dict, *, turns: int = 4, **overrides) -> StageResult:
    base = dict(
        ok=True,
        data=data,
        raw="{}",
        usage=Usage(input_tokens=10, output_tokens=20, cost_usd=0.05, source="usage"),
        error=None,
        turns=turns,
    )
    base.update(overrides)
    return StageResult(**base)


def _failed(error: str = "the CLI is not installed", **overrides) -> StageResult:
    base = dict(ok=False, data=None, raw="", usage=Usage(), error=error)
    base.update(overrides)
    return StageResult(**base)


@pytest.fixture
def ctx(tmp_path):
    # Long enough that the fixture finding's `app.py:12` is a real address.
    # The first version of this file was two lines, and the subject check
    # correctly rejected every finding in it -- the test data was making a
    # claim about the world that the world did not support.
    lines = ['"""A small module with a real defect."""', ""]
    lines += [f"CONST_{n} = {n}" for n in range(1, 10)]
    lines += ["", "def add(values):", "    return values[0]", ""]
    (tmp_path / "app.py").write_text("\n".join(lines), encoding="utf-8")
    return RunContext(
        project_root=tmp_path,
        state_root=tmp_path / ".whetstone",
        files=(Path("app.py"),),
        tier="deep",
        lens_options={"options": {"angles": ["error handling"]}},
        run_id="r1",
    )


# --- what comes out -------------------------------------------------------------


def test_a_schema_valid_payload_becomes_candidates(ctx):
    provider = _FakeProvider(_ok({"findings": [_FINDING], "notes": None}))
    result = hunt(ctx, provider)

    assert result.skips == ()
    assert len(result.candidates) == 1
    assert result.candidates[0]["subject"] == "app.py:12"
    assert result.candidates[0]["observation"] == _FINDING["observation"]
    assert result.candidates[0]["root_cause_hypothesis"] == _FINDING["root_cause_hypothesis"]


def test_one_stage_runs_per_angle(ctx):
    ctx.lens_options["options"]["angles"] = ["error handling", "resource leaks"]
    provider = _FakeProvider(
        _ok({"findings": [_FINDING], "notes": None}),
        _ok({"findings": [], "notes": "read app.py, nothing on leaks"}),
    )
    result = hunt(ctx, provider)

    assert len(provider.requests) == 2
    assert len(result.candidates) == 1


def test_the_angles_do_not_overlap(ctx):
    """SEQUENTIAL, proven by blocking rather than by counting.

    Counting `provider.requests` at the end cannot tell sequential from
    concurrent -- both end with two. This holds the first stage open and
    asserts the second has not started, which is the only shape a concurrent
    implementation fails.

    It matters because the budget is run-level: `--max-budget-usd` below about
    $0.35 makes a stage a no-op, so the ceiling can only stop BETWEEN stages.
    Firing angles concurrently commits their cost before it can react.
    """
    ctx.lens_options["options"]["angles"] = ["one", "two"]
    first_entered = threading.Event()
    release_first = threading.Event()
    entered: list[str] = []

    class _BlockingProvider:
        name = "blocking"

        def __init__(self) -> None:
            self.requests: list[StageRequest] = []

        def run_stage(self, request: StageRequest) -> StageResult:
            self.requests.append(request)
            entered.append(request.prompt)
            if len(entered) == 1:
                first_entered.set()
                assert release_first.wait(timeout=10), "the test released nothing"
            return _ok({"findings": [], "notes": "nothing"})

    provider = _BlockingProvider()
    worker = threading.Thread(target=hunt, args=(ctx, provider), daemon=True)
    worker.start()
    try:
        assert first_entered.wait(timeout=10), "the first stage never started"
        # The first stage is still inside run_stage here.
        assert len(entered) == 1, (
            f"a second stage started while the first was still running: {len(entered)}"
        )
    finally:
        release_first.set()
        worker.join(timeout=10)

    assert len(entered) == 2, "the second stage never ran"


def test_every_candidate_records_which_angle_found_it(ctx):
    """Task 10 has to be able to ask whether one angle produces all the junk."""
    provider = _FakeProvider(_ok({"findings": [_FINDING], "notes": None}))
    result = hunt(ctx, provider)
    assert result.candidates[0]["provenance"]["angle"] == "error handling"


# --- finding nothing ------------------------------------------------------------


def test_an_empty_findings_list_is_accepted_and_never_retried(ctx):
    """Returning zero is a valid answer. Retrying it is how a tool teaches
    itself to manufacture findings."""
    provider = _FakeProvider(_ok({"findings": [], "notes": "read app.py, it is fine"}))
    result = hunt(ctx, provider)

    assert result.candidates == ()
    assert len(provider.requests) == 1, "the empty answer was retried"
    assert result.skips == (), "an honest empty answer is not a skip -- the check ran"


def test_the_reason_for_an_empty_hunt_reaches_the_caller(ctx):
    """The schema requires `notes` exactly when `findings` is empty, so the
    reason always exists. Dropping it on the floor would put the tool back
    where an empty result is indistinguishable from a declined one."""
    provider = _FakeProvider(_ok({"findings": [], "notes": "could not read src/"}))
    result = hunt(ctx, provider)
    # recorded against the run rather than as a skip
    assert any("could not read src/" in note for note in result.notes)


# --- judging the stage, because the provider will not ----------------------------


def test_a_provider_failure_is_a_skip_and_produces_nothing(ctx):
    provider = _FakeProvider(_failed("the `claude` CLI is not installed"))
    result = hunt(ctx, provider)

    assert result.candidates == ()
    assert len(result.skips) == 1
    assert "not installed" in result.skips[0]


def test_a_worktree_mutation_discards_the_candidates(ctx):
    """A read-only stage that wrote is not a stage whose findings we trust.
    The payload may be perfectly well-formed; that is not the question."""
    provider = _FakeProvider(
        _ok({"findings": [_FINDING], "notes": None}, mutation="app.py changed")
    )
    result = hunt(ctx, provider)

    assert result.candidates == ()
    assert len(result.skips) == 1
    assert "app.py changed" in result.skips[0]


def test_a_refused_tool_discards_the_candidates(ctx):
    """A stage refused the tool it needed answered on less than it asked for.

    THE RESULT IS `ok=True` ON PURPOSE. The provider currently also turns a
    non-empty `permission_denials` into a failure, so an `ok=False` fixture
    reaches the same skip through the `not result.ok` branch and proves nothing
    about this one -- the first version of this test did exactly that, and put
    the word "Bash" in the error string as well, so the assertion held through
    the fallthrough too.

    `StageResult(ok=True, denials=...)` is a legal construction, and this branch
    is what keeps the lens correct if the provider ever stops refusing on
    denials by itself.
    """
    provider = _FakeProvider(
        _ok({"findings": [_FINDING], "notes": None}, denials=("Bash", "Write"))
    )
    result = hunt(ctx, provider)

    assert result.candidates == ()
    assert len(result.skips) == 1
    assert "Bash" in result.skips[0] and "Write" in result.skips[0]


def test_a_single_turn_hunt_is_recorded_but_NOT_discarded(ctx):
    """One turn means no tool was called, so nothing was read -- which is what
    a fabricated finding looks like. It is recorded rather than dropped: the
    correlation with junk is a thing Task 10 measures, and discarding on it
    now would destroy the evidence needed to decide."""
    provider = _FakeProvider(_ok({"findings": [_FINDING], "notes": None}, turns=1))
    result = hunt(ctx, provider)

    assert len(result.candidates) == 1, "a suspicious finding is still a finding for now"
    assert result.candidates[0]["provenance"]["read_nothing"] is True
    assert result.skips == ()


def test_a_normal_hunt_is_not_flagged_as_having_read_nothing(ctx):
    provider = _FakeProvider(_ok({"findings": [_FINDING], "notes": None}, turns=6))
    result = hunt(ctx, provider)
    assert result.candidates[0]["provenance"]["read_nothing"] is False


def test_cost_is_recorded_per_candidate_from_total_tokens(ctx):
    """Never `input_tokens`: measured 4 against 41,036 on the same call."""
    provider = _FakeProvider(_ok({"findings": [_FINDING], "notes": None}))
    result = hunt(ctx, provider)
    assert result.candidates[0]["provenance"]["cost_usd"] == pytest.approx(0.05)
    assert result.candidates[0]["provenance"]["tokens"] == 30


# --- what gets sent -------------------------------------------------------------


def test_the_prompt_comes_from_the_file_not_from_python(ctx):
    """A prompt built in code cannot be diffed or reviewed. This asserts the
    file's own words arrive in the request."""
    provider = _FakeProvider(_ok({"findings": [], "notes": "n"}))
    hunt(ctx, provider)

    sent = provider.requests[0].prompt
    marker = "Finding nothing is a real answer"
    assert marker in load_prompt("hunt"), "the fixture's premise moved"
    assert marker in sent


def test_the_angle_and_the_files_are_substituted_in(ctx):
    provider = _FakeProvider(_ok({"findings": [], "notes": "n"}))
    hunt(ctx, provider)

    sent = provider.requests[0].prompt
    assert "error handling" in sent
    assert "app.py" in sent
    assert "$angle" not in sent and "$files" not in sent


def test_the_stage_runs_under_the_hunt_profile_with_no_shell(ctx):
    """Asserted here as well as in the policy tests, because this is the call
    site that could pass the wrong profile."""
    provider = _FakeProvider(_ok({"findings": [], "notes": "n"}))
    hunt(ctx, provider)

    perms = provider.requests[0].permissions
    assert perms.available_tools == frozenset({"Read", "Grep", "Glob"})
    assert "Bash" not in perms.available_tools


def test_the_request_carries_the_hunt_schema_and_the_project_root(ctx):
    provider = _FakeProvider(_ok({"findings": [], "notes": "n"}))
    hunt(ctx, provider)

    request = provider.requests[0]
    assert request.stage == "hunt"
    assert request.cwd == ctx.project_root
    assert "findings" in request.schema["properties"]
    assert request.max_budget_usd is None, (
        "a per-stage budget below about $0.35 makes the stage a guaranteed "
        "no-op; the ceiling is run-level"
    )


# --- the subject is a claim about the world, so it is checked against it ---------


def test_a_fabricated_subject_is_discarded_with_a_reason(ctx):
    """The model controls `subject` completely, and the prompt telling it to
    use a path from the list is an instruction rather than a control. A
    fabricated path would otherwise reach the user as a finding's address --
    and `read_nothing` shows a stage that called no tool at all can produce
    one."""
    finding = {**_FINDING, "subject": "does_not_exist.py:3"}
    provider = _FakeProvider(_ok({"findings": [finding], "notes": None}))
    result = hunt(ctx, provider)

    assert result.candidates == ()
    assert len(result.skips) == 1
    assert "does_not_exist.py" in result.skips[0]


def test_a_subject_outside_the_scope_is_discarded(ctx):
    """In the worktree but not in scope for this run: the finding cannot be
    placed, and reporting it would claim the run covered something it did not."""
    (ctx.project_root / "other.py").write_text("x = 1\n", encoding="utf-8")
    finding = {**_FINDING, "subject": "other.py"}
    provider = _FakeProvider(_ok({"findings": [finding], "notes": None}))
    result = hunt(ctx, provider)

    assert result.candidates == ()
    assert "not in scope" in result.skips[0]


def test_a_line_number_past_the_end_of_the_file_is_discarded(ctx):
    """A finding addressed to line 900 of a file with far fewer does not have
    the address it claims, whatever else is true of it."""
    finding = {**_FINDING, "subject": "app.py:900"}
    provider = _FakeProvider(_ok({"findings": [finding], "notes": None}))
    result = hunt(ctx, provider)

    assert result.candidates == ()
    assert "line 900" in result.skips[0]


def test_a_line_range_subject_is_placed_rather_than_discarded(ctx):
    """A RANGE USED TO BE SWALLOWED WHOLE. `app.py:11-13` failed the all-digit
    test, so the entire string became the filename, matched nothing in scope,
    and a real finding was thrown away -- while the message blamed the user's
    `boundaries.include`. Nothing in the hunt prompt asks for a single line, so
    which format the model picked decided whether the finding survived."""
    finding = {**_FINDING, "subject": "app.py:11-13"}
    provider = _FakeProvider(_ok({"findings": [finding], "notes": None}))
    result = hunt(ctx, provider)

    assert len(result.candidates) == 1, result.skips
    assert result.skips == ()
    # VERBATIM. `dedupe_key` hashes the subject, so rewriting it to `app.py:11`
    # here would re-point every stored rejection at a key it was never filed
    # under. Placing a finding and keying it are different jobs.
    assert result.candidates[0]["subject"] == "app.py:11-13"


def test_a_range_running_past_the_end_of_the_file_is_discarded(ctx):
    """Beginning inside the file does not buy the rest of the span. The message
    repeats the range it was handed rather than a normalised one."""
    finding = {**_FINDING, "subject": "app.py:12-900"}
    provider = _FakeProvider(_ok({"findings": [finding], "notes": None}))
    result = hunt(ctx, provider)

    assert result.candidates == ()
    assert "lines 12-900" in result.skips[0]
    assert "not in scope" not in result.skips[0], (
        "the old failure blamed scope for what is an addressing problem"
    )


def test_a_backwards_range_is_discarded(ctx):
    finding = {**_FINDING, "subject": "app.py:13-11"}
    provider = _FakeProvider(_ok({"findings": [finding], "notes": None}))
    result = hunt(ctx, provider)

    assert result.candidates == ()
    assert "ends before it starts" in result.skips[0]


@pytest.mark.parametrize(
    ("subject", "expected"),
    [
        ("app.py", ("app.py", None)),
        ("app.py:12", ("app.py", (12, 12))),
        ("app.py:14-17", ("app.py", (14, 17))),
        ("app.py:14-14", ("app.py", (14, 14))),
        # NOT AN ADDRESS, so the whole string stays the path. A trailing hyphen,
        # a negative, a word, or a version-like tail must not be mistaken for a
        # span -- guessing here invents a file that was never named.
        ("app.py:14-", ("app.py:14-", None)),
        ("app.py:-17", ("app.py:-17", None)),
        ("app.py:14-17-20", ("app.py:14-17-20", None)),
        ("app.py:abc", ("app.py:abc", None)),
        ("weird-name.py", ("weird-name.py", None)),
        # A Windows drive letter is why this never split on the FIRST colon.
        ("C:/x/app.py", ("C:/x/app.py", None)),
    ],
)
def test_split_subject_parses_only_what_is_actually_an_address(subject, expected):
    from whetstone.lenses.code_defects.hunt import _split_subject

    assert _split_subject(subject) == expected


def test_a_path_escaping_the_project_is_discarded(ctx):
    finding = {**_FINDING, "subject": "../../../etc/passwd"}
    provider = _FakeProvider(_ok({"findings": [finding], "notes": None}))
    result = hunt(ctx, provider)

    assert result.candidates == ()
    assert "inside the project" in result.skips[0]


@pytest.mark.parametrize("subject", ["app.py", "app.py:1", "app.py:2"])
def test_a_real_subject_survives(subject, ctx):
    """The check must not be so strict that a correct finding cannot pass it."""
    finding = {**_FINDING, "subject": subject}
    provider = _FakeProvider(_ok({"findings": [finding], "notes": None}))
    result = hunt(ctx, provider)

    assert len(result.candidates) == 1, result.skips
    assert result.skips == ()


def test_one_bad_subject_does_not_discard_the_good_ones(ctx):
    provider = _FakeProvider(
        _ok(
            {
                "findings": [{**_FINDING, "subject": "ghost.py"}, _FINDING],
                "notes": None,
            }
        )
    )
    result = hunt(ctx, provider)

    assert len(result.candidates) == 1
    assert result.candidates[0]["subject"] == "app.py:12"
    assert len(result.skips) == 1
