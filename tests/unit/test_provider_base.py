from pathlib import Path

import pytest

from whetstone.policy.profiles import profile_for
from whetstone.provider.base import Provider, StageRequest, StageResult, Usage


def _request(**overrides) -> StageRequest:
    base = dict(
        stage="hunt",
        prompt="find bugs",
        schema={"type": "object"},
        permissions=profile_for("hunt"),
        effort="medium",
        max_budget_usd=None,
        cwd=Path("."),
    )
    base.update(overrides)
    return StageRequest(**base)


def test_usage_defaults_to_zero_not_none():
    """A stage that reported no usage costs nothing, it does not cost unknown."""
    usage = Usage()
    assert usage.input_tokens == 0
    assert usage.output_tokens == 0
    assert usage.wall_seconds == 0.0


def test_an_unknown_cost_is_none_and_not_zero():
    """The one field where the rule inverts, and the docstring says so: tokens
    default to zero because zero is a real count, but an unmeasured cost that
    defaults to 0.0 silently under-reports every stage that could not be
    priced. Nothing pinned it, so `cost_usd: float | None = 0.0` constructed."""
    assert Usage().cost_usd is None


def test_usage_with_no_numbers_admits_it_has_no_source():
    """`source` defaulting to `"usage"` would label an empty Usage as having
    come from an envelope block it never read."""
    assert Usage().source == "none"


def test_a_failed_result_carries_a_reason():
    result = StageResult(ok=False, data=None, raw="", usage=Usage(), error="refused")
    assert result.error == "refused"


def test_a_failed_result_must_not_claim_data():
    """ok=False with data set is a contradiction the spine would act on."""
    with pytest.raises(ValueError, match="data"):
        StageResult(ok=False, data={"x": 1}, raw="", usage=Usage(), error="refused")


def test_an_ok_result_must_carry_a_reason_free_payload():
    with pytest.raises(ValueError, match="error"):
        StageResult(ok=True, data={"x": 1}, raw="", usage=Usage(), error="boom")


def test_a_failure_with_no_reason_is_refused():
    """The shape this repo bans everywhere else: a path that declines to do
    work and says nothing about why. It constructed."""
    with pytest.raises(ValueError, match="reason"):
        StageResult(ok=False, data=None, raw="", usage=Usage(), error=None)


def test_denials_and_mutation_default_to_the_quiet_case():
    """Defaults matter here: a provider that forgets to pass them must report
    'nothing was refused and nothing changed', which is a claim, not a gap."""
    result = StageResult(ok=True, data={}, raw="{}", usage=Usage(), error=None)
    assert result.denials == ()
    assert result.mutation is None


def test_request_is_immutable():
    import dataclasses

    with pytest.raises(dataclasses.FrozenInstanceError):
        _request().prompt = "elsewhere"


class _Fake:
    name = "fake"

    def run_stage(self, request: StageRequest) -> StageResult:
        return StageResult(ok=True, data={}, raw="{}", usage=Usage(), error=None)


def test_fake_satisfies_the_protocol():
    assert isinstance(_Fake(), Provider)


def test_the_protocol_requires_a_name_as_well_as_run_stage():
    """`registry.register` leans on this isinstance check, and a
    `runtime_checkable` Protocol only verifies that the ATTRIBUTES exist -- so
    dropping `name: str` from the Protocol silently widened what the registry
    accepts, and nothing caught it. A nameless provider cannot be keyed."""

    class _Nameless:
        def run_stage(self, request: StageRequest) -> StageResult:  # pragma: no cover
            raise AssertionError("never called")

    assert not isinstance(_Nameless(), Provider)
