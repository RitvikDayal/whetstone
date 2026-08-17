import dataclasses
from pathlib import Path

import pytest

from whetstone.errors import LensError
from whetstone.lenses.base import (
    Candidate,
    Evidence,
    EvidenceKind,
    Grade,
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


def test_a_candidate_carries_no_grade_by_default():
    """Most lenses never grade. `hygiene` measures a threshold and is done."""
    candidate = _candidate()
    assert candidate.grade is None
    assert candidate.grade_reason is None


def test_a_valid_grade_with_its_reason_is_accepted():
    candidate = _candidate(grade=Grade.A, grade_reason="graded A: reproduced.")
    assert candidate.grade is Grade.A


@pytest.mark.parametrize("value", ["A", "a", 1, True, object()])
def test_grade_must_be_the_enum_not_something_that_stringifies(value):
    """Issue #9's shape, one field over.

    The store writes `str(grade)`, so 'a' and 'sev-1' would land in the column
    as plausible-looking text that no filter matches and no reader can rank.
    """
    with pytest.raises(LensError, match="grade"):
        _candidate(grade=value, grade_reason="because")


def test_a_grade_without_a_reason_is_refused():
    """A bare letter is a verdict the user cannot check.

    `grade_finding` returns the pair and always has; there is no honest caller
    with one and not the other. Refusing here is what stops a later lens from
    inventing one.
    """
    with pytest.raises(LensError, match="grade_reason"):
        _candidate(grade=Grade.D)


def test_a_reason_without_a_grade_is_refused():
    """The inverse, and it is the likelier typo: a reason explaining nothing."""
    with pytest.raises(LensError, match="grade"):
        _candidate(grade_reason="graded D: the falsifier killed it.")


def test_the_grade_is_not_part_of_the_dedupe_key():
    """Same argument as title and severity.

    A finding re-graded from D to A is the SAME finding, and if the grade
    entered the key a re-grade would mint a new row -- which is exactly how a
    rejection gets resurrected.
    """
    assert _candidate(grade=Grade.A, grade_reason="r").dedupe_key == (
        _candidate(grade=Grade.D, grade_reason="r").dedupe_key
    )


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


# --- leaf-field validation (issues #14 and #9) --------------------------------
#
# One validation point, because the two issues are the same defect seen twice: a
# lens hands the spine a field the store cannot use. Validating at `Candidate`
# rather than per-detector means every lens inherits it, including third-party
# packs that arrive through the entry point and never saw this file.


@pytest.mark.parametrize("field_name", ["lens", "rule_id", "subject"])
@pytest.mark.parametrize("value", [None, 123, 1.5, True, b"bytes", ["a"], {"a": 1}])
def test_a_dedupe_component_that_is_not_text_is_refused(field_name, value):
    """`dedupe_key` JSON-encodes these three, so a non-string silently changes
    the hash instead of failing -- the finding is stored under an identity
    nothing else will ever produce again."""
    with pytest.raises(LensError, match=field_name):
        _candidate(**{field_name: value})


@pytest.mark.parametrize("field_name", ["title", "detail"])
@pytest.mark.parametrize("value", [None, 123, ["a"]])
def test_prose_that_is_not_text_is_refused(field_name, value):
    """`lens`, `rule_id`, `title` and `detail` are NOT NULL columns, so a None
    reached sqlite as an IntegrityError three layers from the lens that caused
    it."""
    with pytest.raises(LensError, match=field_name):
        _candidate(**{field_name: value})


@pytest.mark.parametrize("field_name", ["lens", "rule_id", "subject", "title", "detail"])
@pytest.mark.parametrize("value", ["", "   ", "\n"])
def test_an_empty_text_field_is_refused(field_name, value):
    """An empty subject is a finding about nothing and an empty rule_id makes
    every finding from that lens one finding."""
    with pytest.raises(LensError, match=field_name):
        _candidate(**{field_name: value})


def test_severity_none_is_refused_rather_than_laundered(tmp_path):
    """Issue #9. `upsert` does `str(candidate.severity)`, so a None used to be
    stored as the STRING 'None': the row is queued, `list_findings` sorts it
    into the ELSE bucket, and nothing anywhere says it is wrong. Every other
    None from a lens fails loudly; this was the one that became plausible data."""
    with pytest.raises(LensError, match="severity"):
        _candidate(severity=None)


@pytest.mark.parametrize("value", ["high", "HIGH", "sev-1", 3, None])
def test_severity_must_be_the_enum_not_something_that_stringifies(value):
    """A model will produce 'HIGH' or 'sev-1'. Both stringify to text sqlite
    accepts and `list_findings`'s CASE ranks below 'medium'."""
    with pytest.raises(LensError, match="severity"):
        _candidate(severity=value)


def test_a_valid_severity_enum_is_accepted():
    for member in Severity:
        assert _candidate(severity=member).severity is member


@pytest.mark.parametrize("value", [None, "metric", {"kind": "metric"}])
def test_evidence_must_be_the_dataclass_not_a_lookalike(value):
    """The store calls `candidate.evidence.to_json()`. Anything else raises
    AttributeError inside the transaction, after the run row already exists."""
    with pytest.raises(LensError, match="evidence"):
        _candidate(evidence=value)


def test_evidence_kind_must_be_the_enum():
    with pytest.raises(LensError, match="kind"):
        Evidence("metric", "s", {"k": 1})


def test_evidence_data_must_be_a_mapping():
    with pytest.raises(LensError, match="data"):
        Evidence(EvidenceKind.metric, "s", ["k", 1])


def test_evidence_data_must_be_json_encodable():
    """`to_json` runs inside the store's transaction. A TypeError there aborts
    a run over a payload the lens could have been told about at construction."""
    with pytest.raises(LensError, match="data"):
        Evidence(EvidenceKind.metric, "s", {"when": object()})


@pytest.mark.parametrize(
    "data",
    [
        {1: "value"},
        {True: "value"},
        {None: "value"},
        {"outer": {2: "value"}},
        {"outer": [{"inner": {3.5: "value"}}]},
    ],
    ids=["int", "bool", "none", "nested-dict", "nested-through-a-list"],
)
def test_evidence_data_refuses_a_non_string_mapping_key(data):
    """`json.dumps` does not refuse these, it renames them: `{1: "v"}` stores as
    `{"1": "v"}` and `{True: "v"}` as `{"true": "v"}`. The document read back out
    is a different document from the one the lens handed over, and a uniform
    int-keyed mapping sorts cleanly under `sort_keys=True`, so the serialiser
    never raises by accident either."""
    with pytest.raises(LensError, match="key"):
        Evidence(EvidenceKind.metric, "s", data)


def test_to_json_serialises_the_state_that_was_validated():
    """`data` is a mutable dict inside a frozen dataclass, so a lens can put an
    unserialisable object in after construction. Serialising then failed inside
    the store's transaction, which is exactly what validating at construction
    exists to prevent -- the check was real and the window after it was open."""
    evidence = Evidence(EvidenceKind.metric, "s", {"pct": 1.0})
    evidence.data["when"] = object()
    assert evidence.to_json() == (
        '{"artifacts": [], "data": {"pct": 1.0}, "kind": "metric", "summary": "s"}'
    )


def test_a_mutated_artifacts_list_does_not_reach_the_store():
    """The same window, through the other field. A list passed for `artifacts`
    stays the caller's list."""
    artifacts = ["repro.py"]
    evidence = Evidence(EvidenceKind.metric, "s", {}, artifacts=artifacts)
    artifacts.append(object())
    assert '"artifacts": ["repro.py"]' in evidence.to_json()


def test_evidence_artifacts_must_be_a_sequence_of_text():
    with pytest.raises(LensError, match="artifacts"):
        Evidence(EvidenceKind.metric, "s", {}, artifacts=(1, 2))


def test_a_string_artifacts_value_is_refused_not_split_into_characters():
    """`artifacts="a.txt"` is iterable, so `list(...)` turns one path into eight
    single-character ones without raising."""
    with pytest.raises(LensError, match="artifacts"):
        Evidence(EvidenceKind.metric, "s", {}, artifacts="a.txt")


def test_the_validation_error_names_the_lens_and_the_field():
    """A LensError that does not say which lens and which field sends the
    reader to the spine to look for a bug the lens caused."""
    with pytest.raises(LensError) as caught:
        _candidate(lens="demo", severity=None)
    message = str(caught.value)
    assert "demo" in message
    assert "severity" in message


def test_well_formed_candidates_are_still_accepted():
    """The counterweight to every refusal above.

    Named for what it does. It used to be called
    `test_every_first_party_candidate_construction_site_still_validates`, which
    claimed to inspect the real construction sites in `detectors/`; it does not,
    it hand-builds two candidates. The real coverage of those sites is in
    `test_hygiene.py` and `test_deps_subprocess.py`, which run the detectors.
    A test that passes for a reason other than the one its name gives is the
    pattern this project has been bitten by repeatedly, so the name moved to
    meet the test rather than the other way round.
    """
    built = [
        _candidate(),
        Candidate(
            lens="hygiene",
            rule_id="COVERAGE",
            subject="project",
            title="t",
            detail="d",
            severity=Severity.medium,
            evidence=Evidence(EvidenceKind.metric, "s", {"pct": 1.0}),
        ),
    ]
    assert built, "nothing was constructed, so this test asserts nothing"
    for candidate in built:
        assert candidate.dedupe_key


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


# --- skips are append-only through skip() (issue #15) -------------------------


def _ctx(tmp_path) -> RunContext:
    return RunContext(
        project_root=tmp_path,
        state_root=tmp_path / "state",
        files=(),
        tier="quick",
        lens_options={},
        run_id="run-1",
    )


def test_skip_is_the_only_writer(tmp_path):
    ctx = _ctx(tmp_path)
    ctx.skip("a")
    ctx.skip("b")
    assert list(ctx.skips) == ["a", "b"]


def test_a_lens_cannot_erase_its_own_trail(tmp_path):
    """`ctx.skips.clear()` and `ctx.skips[:] = []` were both live: the list was
    the attribute, so a lens could report work not done and then unreport it."""
    ctx = _ctx(tmp_path)
    ctx.skip("pip-audit did not run")
    with pytest.raises(AttributeError):
        ctx.skips.clear()
    with pytest.raises(TypeError):
        ctx.skips[:] = []
    assert list(ctx.skips) == ["pip-audit did not run"]


def test_a_lens_cannot_forge_an_entry(tmp_path):
    ctx = _ctx(tmp_path)
    with pytest.raises(AttributeError):
        ctx.skips.append("nothing was wrong")
    assert list(ctx.skips) == []


def test_a_lens_cannot_rebind_the_attribute(tmp_path):
    """Rebinding was the simplest erasure of all: `ctx.skips = []`."""
    ctx = _ctx(tmp_path)
    ctx.skip("real")
    with pytest.raises(AttributeError):
        ctx.skips = []
    assert list(ctx.skips) == ["real"]


def test_the_view_is_a_snapshot_not_a_live_handle(tmp_path):
    """A view handed out before a later skip must not be a route back into the
    list -- that is the same mutable handle wearing a property."""
    ctx = _ctx(tmp_path)
    ctx.skip("first")
    view = ctx.skips
    ctx.skip("second")
    assert list(view) == ["first"]
    assert list(ctx.skips) == ["first", "second"]


def test_a_skip_reason_must_be_text(tmp_path):
    """A skip is rendered into the console and the HTML report. A non-string
    reaches those as whatever it stringifies to, or as a template error."""
    ctx = _ctx(tmp_path)
    with pytest.raises(LensError, match="skip"):
        ctx.skip(None)
    with pytest.raises(LensError, match="skip"):
        ctx.skip("")
    assert list(ctx.skips) == []


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
