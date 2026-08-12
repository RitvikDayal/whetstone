from pathlib import Path

import pytest

from whetstone.provider.base import Provider, StageRequest, StageResult, Usage


def _request(**overrides) -> StageRequest:
    base = dict(
        stage="hunt",
        prompt="find bugs",
        schema={"type": "object"},
        permissions=None,
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
