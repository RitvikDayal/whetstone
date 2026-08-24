"""The run-level budget.

EVERY STUB HERE SPENDS REAL MONEY. A ceiling test whose provider returns
`Usage()` passes whether or not the ceiling works -- nothing was ever spent, so
nothing could exhaust. So every fake below returns a `Usage` with a non-trivial
cost AND the measured cache shape: `input_tokens=4` alongside
`cache_creation_input_tokens=41036`, which is the real envelope that made a
budget reading `input_tokens` under-report by four orders of magnitude.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from whetstone.budget import (
    Budget,
    BudgetedProvider,
    StageCost,
    _cost_filename,
    write_cost_record,
)
from whetstone.policy.profiles import profile_for
from whetstone.provider.base import StageRequest, StageResult, Usage

# The measured envelope. Not invented: 4 input tokens next to 41,036
# cache-creation tokens on the same call.
_MEASURED = Usage(
    input_tokens=4,
    output_tokens=120,
    cache_creation_input_tokens=41036,
    cache_read_input_tokens=900,
    cost_usd=0.0921,
    wall_seconds=11.3,
    source="usage",
)


def _request(stage: str = "hunt") -> StageRequest:
    return StageRequest(
        stage=stage,
        prompt="look",
        schema={"type": "object"},
        permissions=profile_for(stage),
        effort="medium",
        max_budget_usd=None,
        cwd=Path("."),
    )


class _FakeProvider:
    """Returns a real cost every time, and counts how often it was asked."""

    name = "fake"

    def __init__(self, usage: Usage = _MEASURED) -> None:
        self.usage = usage
        self.calls = 0

    def run_stage(self, request: StageRequest) -> StageResult:
        self.calls += 1
        return StageResult(
            ok=True,
            data={"findings": []},
            raw="{}",
            usage=self.usage,
            error=None,
            turns=4,
        )


# --- what spend() reads ---------------------------------------------------------


def test_spend_counts_every_token_field_not_input_tokens():
    """The measured case: 4 against 41,036 on the same call."""
    budget = Budget()
    budget.spend(_MEASURED, stage="hunt", subject="app.py")

    assert budget.tokens == 4 + 120 + 41036 + 900
    assert budget.tokens != _MEASURED.input_tokens


def test_spend_accumulates_cost():
    budget = Budget(ceiling_usd=1.0)
    budget.spend(_MEASURED, stage="hunt", subject="a")
    budget.spend(_MEASURED, stage="falsify", subject="b")

    assert budget.spent_usd == pytest.approx(0.1842)
    assert budget.calls == 2


def test_spend_takes_a_usage_alone():
    """The plan's signature is `spend(usage)`; stage and subject are labels."""
    budget = Budget()
    budget.spend(_MEASURED)

    assert budget.calls == 1
    assert budget.spent_usd == pytest.approx(0.0921)


# --- the ceiling ----------------------------------------------------------------


def test_below_the_ceiling_is_not_exhausted():
    budget = Budget(ceiling_usd=0.50)
    budget.spend(_MEASURED)

    assert budget.exhausted() is False
    assert budget.reason() is None
    assert budget.remaining() == pytest.approx(0.4079)


def test_reaching_the_ceiling_exhausts_it():
    budget = Budget(ceiling_usd=0.15)
    budget.spend(_MEASURED)
    budget.spend(_MEASURED)

    assert budget.exhausted() is True
    assert budget.remaining() == 0.0


def test_landing_exactly_on_the_ceiling_exhausts_it():
    """`>=`, not `>`. A ceiling reached to the cent is reached."""
    budget = Budget(ceiling_usd=0.0921)
    budget.spend(_MEASURED)

    assert budget.exhausted() is True


def test_a_zero_ceiling_is_a_ceiling_not_an_absent_one():
    """0.0 is falsy, and treating it as unset makes `usd_per_run: 0` unbounded."""
    budget = Budget(ceiling_usd=0.0)

    assert budget.exhausted() is True
    assert budget.reason() is not None


def test_no_ceiling_is_never_exhausted():
    budget = Budget()
    for _ in range(50):
        budget.spend(_MEASURED)

    assert budget.exhausted() is False
    assert budget.remaining() is None


def test_the_call_ceiling_stops_it_too():
    budget = Budget(ceiling_calls=2)
    budget.spend(_MEASURED)
    assert budget.exhausted() is False
    budget.spend(_MEASURED)

    assert budget.exhausted() is True
    assert budget.remaining_calls() == 0
    assert "call" in budget.reason()


def test_a_zero_call_ceiling_is_a_ceiling_not_an_absent_one():
    """The same trap as `usd_per_run: 0`, one field over and untested until
    now: `if self.ceiling_calls` reads a declared limit of nothing as no limit
    at all, which is the exact opposite of what it says."""
    budget = Budget(ceiling_calls=0)

    assert budget.exhausted() is True
    assert budget.remaining_calls() == 0
    assert "call" in budget.reason()


def test_the_reason_names_the_spend_and_the_ceiling():
    budget = Budget(ceiling_usd=0.15)
    budget.spend(_MEASURED)
    budget.spend(_MEASURED)

    reason = budget.reason()
    assert "0.18" in reason
    assert "0.15" in reason


# --- an unmeasured cost is not a free one ---------------------------------------


def test_a_stage_that_reported_no_cost_is_counted_as_unmeasured():
    """cost_usd=None means the provider could not measure, not that it was free.
    A ceiling enforced against an under-count has to say so."""
    budget = Budget(ceiling_usd=1.0)
    budget.spend(Usage(input_tokens=4, cache_creation_input_tokens=41036, source="none"))

    assert budget.unmeasured_calls == 1
    assert budget.spent_usd == 0.0
    assert budget.tokens == 41040


# --- the ledger, which is what the estimator is fit to ---------------------------


def test_the_ledger_records_every_stage_with_its_real_numbers():
    budget = Budget()
    budget.spend(_MEASURED, stage="hunt", subject="app.py")
    budget.spend(_MEASURED, stage="falsify", subject="app.py:12")

    assert len(budget.ledger) == 2
    first = budget.ledger[0]
    assert isinstance(first, StageCost)
    assert first.stage == "hunt"
    assert first.subject == "app.py"
    assert first.cost_usd == pytest.approx(0.0921)
    assert first.tokens == 42060
    assert first.wall_seconds == pytest.approx(11.3)
    assert first.source == "usage"
    assert budget.ledger[1].stage == "falsify"


def test_the_ledger_is_a_snapshot_a_caller_cannot_edit():
    budget = Budget()
    budget.spend(_MEASURED)
    ledger = budget.ledger
    budget.spend(_MEASURED)

    assert len(ledger) == 1
    assert len(budget.ledger) == 2


# --- the wrapper, which is where the ceiling actually bites ----------------------


def test_the_wrapper_passes_the_request_through_and_records_what_it_cost():
    inner = _FakeProvider()
    budget = Budget(ceiling_usd=1.0)
    provider = BudgetedProvider(inner, budget)

    result = provider.run_stage(_request())

    assert result.ok is True
    assert inner.calls == 1
    assert budget.spent_usd == pytest.approx(0.0921)
    assert budget.ledger[0].stage == "hunt"


def test_a_failed_stage_is_still_charged():
    """The tokens were billed before the stage decided it had failed.

    `run_stage` spends on every result the inner provider returns, and no test
    asserted that. A regression guarding the `spend` call with `if result.ok`
    would keep every other test in this file green, and a run of failures --
    each burning the 41,036 cache-creation tokens the fixture carries -- would
    spend without limit and record itself as free. That under-count is what
    this module exists to prevent.
    """

    class _FailingProvider:
        name = "fake"

        def run_stage(self, request: StageRequest) -> StageResult:
            return StageResult(
                ok=False,
                data=None,
                raw="",
                usage=_MEASURED,
                error="the model gave up",
            )

    budget = Budget(ceiling_usd=1.0)
    result = BudgetedProvider(_FailingProvider(), budget).run_stage(_request())

    assert result.ok is False
    assert budget.calls == 1
    assert budget.spent_usd == pytest.approx(0.0921)
    assert budget.tokens == 42060
    assert budget.ledger[0].stage == "hunt"


def test_the_wrapper_keeps_the_real_provider_name():
    assert BudgetedProvider(_FakeProvider(), Budget()).name == "fake"


def test_the_wrapper_refuses_once_the_ceiling_is_reached():
    """The stub spends 0.0921 a call, so the second call crosses 0.15."""
    inner = _FakeProvider()
    budget = Budget(ceiling_usd=0.15)
    provider = BudgetedProvider(inner, budget)

    assert provider.run_stage(_request()).ok is True
    assert provider.run_stage(_request()).ok is True
    third = provider.run_stage(_request())

    assert third.ok is False
    assert "budget" in third.error
    assert inner.calls == 2, "the refused call must never reach the provider"


def test_a_refused_call_is_not_counted_against_the_budget():
    """Otherwise a run that stops keeps spending on paper."""
    inner = _FakeProvider()
    budget = Budget(ceiling_usd=0.05)
    provider = BudgetedProvider(inner, budget)

    provider.run_stage(_request())
    before = (budget.spent_usd, budget.calls, len(budget.ledger))
    provider.run_stage(_request())

    assert (budget.spent_usd, budget.calls, len(budget.ledger)) == before


def test_the_wrapper_labels_the_ledger_with_the_subject_it_was_given():
    inner = _FakeProvider()
    budget = Budget()
    provider = BudgetedProvider(inner, budget)
    provider.subject = "app.py:12"

    provider.run_stage(_request("falsify"))

    assert budget.ledger[0].subject == "app.py:12"
    assert budget.ledger[0].stage == "falsify"


def test_a_refusal_is_a_well_formed_failed_result():
    """`StageResult.__post_init__` refuses a failure with no error, so this is
    the shape every stage's `did not run` branch already knows how to report."""
    provider = BudgetedProvider(_FakeProvider(), Budget(ceiling_usd=0.0))

    result = provider.run_stage(_request())

    assert result.ok is False
    assert result.data is None
    assert result.error
    assert result.usage.total_tokens == 0


# --- the cost record --------------------------------------------------------
#
# MOVED HERE FROM `lenses/code_defects/pack.py`, where it was a private method,
# and the move is the point rather than tidiness: `rendered-ui` builds a
# `Budget`, wraps its provider in a `BudgetedProvider`, spends real money
# through it, and wrote no record at all. One writer, called by both.


def _spent(usd: float | None = 0.25, *, calls: int = 1) -> Budget:
    budget = Budget()
    for _ in range(calls):
        # The measured cache shape, per this module docstring: a stub that spends
        # nothing makes every assertion below pass vacuously.
        budget.spend(
            Usage(input_tokens=4, cache_creation_input_tokens=41036, cost_usd=usd),
            stage="hunt",
            subject="a.py",
        )
    return budget


def _read(state_root: Path, name: str) -> dict:
    import json

    return json.loads((state_root / "costs" / name).read_text(encoding="utf-8"))


def test_the_record_is_keyed_by_lens_as_well_as_run(tmp_path):
    """Two lenses in one run must not overwrite each other.

    The first version wrote `<run_id>.json` while carrying a `"lens"` field
    inside it -- a record whose own contents say more than one lens was
    expected, in a filename that can hold exactly one. It was invisible because
    only `code-defects` wrote records; the second lens to do so would have
    silently destroyed the first, and the lost half is money that was spent.
    """
    write_cost_record(
        state_root=tmp_path, run_id="run-1", lens="code-defects", tier="deep",
        budget=_spent(0.25), on_skip=lambda _s: None,
    )
    write_cost_record(
        state_root=tmp_path, run_id="run-1", lens="rendered-ui", tier="deep",
        budget=_spent(0.10), on_skip=lambda _s: None,
    )

    assert _read(tmp_path, "run-1.code-defects.json")["spent_usd"] == pytest.approx(0.25)
    assert _read(tmp_path, "run-1.rendered-ui.json")["spent_usd"] == pytest.approx(0.10)
    assert len(list((tmp_path / "costs").glob("*.json"))) == 2


@pytest.mark.parametrize(
    "lens",
    [
        "../../evil",
        "../" * 8 + "evil",
        "a/b",
        # `"a\\b"`, NOT `"a\b"`. The first version of this list wrote `"a\b"`,
        # which Python reads as `a` followed by U+0008 BACKSPACE -- so the
        # Windows-separator case, on the platform this project targets first,
        # was never tested at all. A parametrised case that does not contain
        # the character it is named for is a row that always passes.
        "a\\b",
        "C:evil",
        "with space",
        "..",
        ".",
    ],
)
def test_no_lens_name_can_put_a_separator_in_the_filename(lens):
    """The sanitiser, tested DIRECTLY, because containment is not enough.

    The first version of this test wrote a record with `lens="../../evil"` and
    asserted the file landed under `costs/`. It passed with the sanitiser
    REMOVED -- the mutation battery caught it. `costs/run-1.../../evil.json`
    resolves back into `costs/` by arithmetic, so containment held while the
    defence was gone, and the test was measuring a coincidence.

    A separator in the name is the thing that turns a lens name into a
    traversal, so that is what is asserted, on the function that decides it.
    """
    name = _cost_filename("run-1", lens)

    assert "/" not in name
    assert "\\" not in name
    assert Path(name).name == name
    assert name.startswith("run-1.") and name.endswith(".json")


def test_a_lens_name_cannot_escape_the_costs_directory(tmp_path):
    """And the end-to-end half: the bytes land under `costs/` and nowhere else."""
    write_cost_record(
        state_root=tmp_path, run_id="run-1", lens="../" * 8 + "evil", tier="deep",
        budget=_spent(), on_skip=lambda _s: None,
    )

    everywhere = list(tmp_path.rglob("*.json"))
    assert len(everywhere) == 1
    assert everywhere[0].resolve().parent == (tmp_path / "costs").resolve()


def test_two_lens_names_that_sanitise_alike_still_get_two_files(tmp_path):
    """Sanitising alone would silently merge them, which is the same defect."""
    for lens in ("a/b", "a:b"):
        write_cost_record(
            state_root=tmp_path, run_id="run-1", lens=lens, tier="deep",
            budget=_spent(), on_skip=lambda _s: None,
        )

    assert len(list((tmp_path / "costs").glob("*.json"))) == 2


def test_a_lens_that_spent_nothing_still_writes_a_record(tmp_path):
    """`calls: 0` is a fact. Absence is three different facts at once.

    The earlier version skipped an empty ledger, reasoning that "an empty
    record would read as a run that cost nothing rather than one that never
    called a model". That is backwards: a record saying `calls: 0` states
    exactly that the lens ran and made no billable call. ABSENCE is what
    collapses "not configured", "declined before building a budget" and "ran
    and spent nothing" into one silence -- and a cost surface then cannot tell
    $0.00 from unmeasured.
    """
    write_cost_record(
        state_root=tmp_path, run_id="run-1", lens="code-defects", tier="quick",
        budget=Budget(), on_skip=lambda _s: None,
    )

    record = _read(tmp_path, "run-1.code-defects.json")
    assert record["calls"] == 0
    assert record["spent_usd"] == 0.0
    assert record["stages"] == []


def test_an_unmeasured_call_is_reported_as_unmeasured_not_as_free(tmp_path):
    skips: list[str] = []
    write_cost_record(
        state_root=tmp_path, run_id="run-1", lens="code-defects", tier="deep",
        budget=_spent(None), on_skip=skips.append,
    )

    assert _read(tmp_path, "run-1.code-defects.json")["unmeasured_calls"] == 1
    assert any("known to be short" in s for s in skips)


def test_a_record_that_cannot_be_written_is_reported_rather_than_swallowed(tmp_path):
    """Spend that happened and was not recorded has to reach the user."""
    blocked = tmp_path / "costs"
    blocked.write_text("not a directory", encoding="utf-8")
    skips: list[str] = []

    write_cost_record(
        state_root=tmp_path, run_id="run-1", lens="code-defects", tier="deep",
        budget=_spent(0.25), on_skip=skips.append,
    )

    assert any("spent and not recorded" in s for s in skips)


def test_lens_names_differing_only_in_case_get_different_files():
    """Windows and macOS fold case, so `Foo` and `foo` are ONE file there.

    Both survive character sanitisation unchanged, so the `safe != lens` check
    alone let them collide -- and a collision here is a lens's whole spend
    silently overwritten by another's. Asserted on the filename rather than by
    writing two files, because on a case-SENSITIVE filesystem writing them
    would pass while the Windows and macOS behaviour stayed broken.
    """
    assert _cost_filename("run-1", "Foo") != _cost_filename("run-1", "foo")
    assert (
        _cost_filename("run-1", "Foo").lower()
        != _cost_filename("run-1", "foo").lower()
    ), "the names must differ AFTER case folding, or the filesystem merges them"


def test_the_ordinary_lens_names_keep_their_plain_filenames():
    """The digest is for names that need disambiguating. `code-defects` and
    `rendered-ui` are already lower-case and already safe, and a hash suffix on
    every record would make the directory unreadable for no reason."""
    assert _cost_filename("run-1", "code-defects") == "run-1.code-defects.json"
    assert _cost_filename("run-1", "rendered-ui") == "run-1.rendered-ui.json"
