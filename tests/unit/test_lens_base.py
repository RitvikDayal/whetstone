import dataclasses
from pathlib import Path

import pytest

from whetstone.errors import LensError
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


def test_dedupe_key_is_the_same_on_windows_and_posix():
    """A rejection recorded on Windows must suppress the same finding on Linux CI."""
    assert _candidate(subject=r"src\a.py").dedupe_key == _candidate(subject="src/a.py").dedupe_key
    assert (
        _candidate(subject=r"src\deep\a.py").dedupe_key
        == _candidate(subject="src/deep/a.py").dedupe_key
    )


def test_dedupe_key_normalisation_does_not_touch_the_displayed_subject():
    assert _candidate(subject=r"src\a.py").subject == r"src\a.py"


def test_dedupe_key_still_separates_genuinely_different_subjects():
    assert _candidate(subject="src/a.py").dedupe_key != _candidate(subject="src/b.py").dedupe_key


def test_dedupe_key_ignores_cosmetic_fields():
    assert _candidate().dedupe_key == _candidate(title="reworded").dedupe_key


def test_dedupe_key_is_not_confused_by_separators_in_components():
    a = _candidate(lens="a|b", rule_id="c", subject="d")
    b = _candidate(lens="a", rule_id="b|c", subject="d")
    assert a.dedupe_key != b.dedupe_key


def test_dedupe_key_handles_a_route_subject_with_a_pipe():
    a = _candidate(subject="/search?q=x|y")
    b = _candidate(subject="/search?q=x", rule_id="R1|y")
    assert a.dedupe_key != b.dedupe_key


def test_severity_ordering():
    assert severity_at_least(Severity.high, Severity.medium)
    assert not severity_at_least(Severity.low, Severity.medium)
    assert severity_at_least(Severity.critical, Severity.critical)


def test_evidence_json_is_deterministic():
    a = Evidence(EvidenceKind.metric, "s", {"b": 2, "a": 1})
    b = Evidence(EvidenceKind.metric, "s", {"a": 1, "b": 2})
    assert a.to_json() == b.to_json()


def test_candidate_is_immutable():
    candidate = _candidate()
    with pytest.raises(dataclasses.FrozenInstanceError):
        candidate.subject = "elsewhere"


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


def test_load_plugins_failure_is_sticky_not_silently_partial(monkeypatch):
    import whetstone.lenses.registry as registry_module

    class _BrokenEntry:
        name = "broken"

        def load(self):
            def _factory():
                raise RuntimeError("boom")

            return _factory

    monkeypatch.setattr(registry_module, "entry_points", lambda group=None: [_BrokenEntry()])
    monkeypatch.setattr(registry_module, "_REGISTRY", {})
    monkeypatch.setattr(registry_module, "_LOADED_PLUGINS", False)
    monkeypatch.setattr(registry_module, "_LOAD_ERROR", None, raising=False)

    with pytest.raises(LensError):
        available_lenses()
    # The second call must still raise — not silently return a partial registry.
    with pytest.raises(LensError):
        available_lenses()
