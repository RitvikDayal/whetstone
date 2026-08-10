from pathlib import Path

import pytest

from whetstone.lenses.base import (
    Candidate,
    Evidence,
    EvidenceKind,
    LensPack,
    RunContext,
    Severity,
    severity_at_least,
)
from whetstone.lenses.registry import available_lenses, get_lens, register


def _candidate(**overrides) -> Candidate:
    base = dict(
        lens="demo",
        rule_id="R1",
        subject="src/a.py",
        title="t",
        detail="d",
        severity=Severity.low,
        evidence=Evidence(EvidenceKind.metric, "s", {"k": 1}),
    )
    base.update(overrides)
    return Candidate(**base)


def test_dedupe_key_is_stable_and_identity_bearing():
    assert _candidate().dedupe_key == _candidate().dedupe_key
    assert _candidate().dedupe_key != _candidate(subject="src/b.py").dedupe_key
    assert _candidate().dedupe_key != _candidate(rule_id="R2").dedupe_key


def test_dedupe_key_ignores_cosmetic_fields():
    assert _candidate().dedupe_key == _candidate(title="reworded").dedupe_key


def test_severity_ordering():
    assert severity_at_least(Severity.high, Severity.medium)
    assert not severity_at_least(Severity.low, Severity.medium)
    assert severity_at_least(Severity.critical, Severity.critical)


def test_evidence_json_is_deterministic():
    a = Evidence(EvidenceKind.metric, "s", {"b": 2, "a": 1})
    b = Evidence(EvidenceKind.metric, "s", {"a": 1, "b": 2})
    assert a.to_json() == b.to_json()


def test_candidate_is_immutable():
    with pytest.raises(Exception):  # noqa: B017 - frozen dataclass, exact type not the point
        _candidate().subject = "elsewhere"


class _Fake:
    name = "fake"
    max_autonomy = 3

    def supports_tier(self, tier: str) -> bool:
        return True

    def run(self, ctx: RunContext):
        yield _candidate(lens="fake")


def test_registry_round_trip():
    register(_Fake())
    assert "fake" in available_lenses()
    assert get_lens("fake").name == "fake"
    assert get_lens("nope") is None


def test_fake_satisfies_the_protocol():
    assert isinstance(_Fake(), LensPack)


def test_run_context_carries_scope(tmp_path):
    ctx = RunContext(
        project_root=tmp_path,
        state_root=tmp_path / "state",
        files=(Path("src/a.py"),),
        tier="quick",
        lens_options={},
        run_id="run-1",
    )
    assert ctx.files == (Path("src/a.py"),)
