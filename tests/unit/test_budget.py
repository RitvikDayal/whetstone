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

from whetstone.budget import Budget, BudgetedProvider, StageCost
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
