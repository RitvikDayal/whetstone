import json
from pathlib import Path

import pytest

import whetstone.lenses.registry as registry_module
from whetstone.config.model import LensConfig, ProjectConfig, WhetstoneConfig
from whetstone.lenses.base import (
    Candidate,
    Evidence,
    EvidenceKind,
    RunContext,
    Severity,
)
from whetstone.lenses.registry import register
from whetstone.runner import execute_run
from whetstone.store.db import connect
from whetstone.store.findings import list_findings


@pytest.fixture(autouse=True)
def _isolated_registry(monkeypatch):
    """Stub lens packs registered below must not leak into other test modules
    sharing this pytest process. Same fix as
    test_load_plugins_failure_is_sticky_not_silently_partial in
    test_lens_base.py: monkeypatch the module-level registry dict itself so
    each test starts from a fresh copy and the original is restored after.
    """
    monkeypatch.setattr(registry_module, "_REGISTRY", dict(registry_module._REGISTRY))


class _Stub:
    name = "stub"
    max_autonomy = 3

    def __init__(self, count: int = 1, tiers=("quick", "standard", "deep")):
        self._count = count
        self._tiers = tiers

    def supports_tier(self, tier: str) -> bool:
        return tier in self._tiers

    def run(self, ctx: RunContext):
        ctx.skip("stub: a deliberate skip")
        for index in range(self._count):
            yield Candidate(
                lens="stub",
                rule_id=f"R{index}",
                subject=f"file{index}.py",
                title="t",
                detail="d",
                severity=Severity.low,
                evidence=Evidence(EvidenceKind.metric, "s", {}),
            )


class _ImmediateBoom:
    """Raises before ever yielding or skipping. Covers the immediate-failure
    half of the finally-always-closes-the-run-row regression test."""

    name = "boom"
    max_autonomy = 3

    def supports_tier(self, tier: str) -> bool:
        return True

    def run(self, ctx: RunContext):
        raise RuntimeError("boom immediately")
        yield  # pragma: no cover - unreachable; makes this a generator function


class _PartialThenBoom:
    """Records a skip, yields one candidate, then raises. Covers both: the
    skip-before-raise loss, and the raise-after-yielding half of the
    finally regression test."""

    name = "partial"
    max_autonomy = 3

    def supports_tier(self, tier: str) -> bool:
        return True

    def run(self, ctx: RunContext):
        ctx.skip("partial: about to explode")
        yield Candidate(
            lens="partial",
            rule_id="R0",
            subject="file0.py",
            title="t",
            detail="d",
            severity=Severity.low,
            evidence=Evidence(EvidenceKind.metric, "s", {}),
        )
        raise RuntimeError("boom mid-lens")


class _Honest:
    """Records one legitimate skip and yields nothing. Used as the first of
    two lenses in a run to check that a later lens cannot erase its trail."""

    name = "honest"
    max_autonomy = 3

    def supports_tier(self, tier: str) -> bool:
        return True

    def run(self, ctx: RunContext):
        ctx.skip("honest: legitimate skip")
        return
        yield  # pragma: no cover - unreachable; makes this a generator function


class _Evil:
    """Clears its own ctx.skips before recording its own. A lens's blast
    radius must stop at its own skip trail -- lens packs become third-party
    code once the plugin API is public, and 'a lens would not do that' is
    not a defence."""

    name = "evil"
    max_autonomy = 3

    def supports_tier(self, tier: str) -> bool:
        return True

    def run(self, ctx: RunContext):
        ctx.skips.clear()
        ctx.skip("evil: pretending nothing happened before me")
        return
        yield  # pragma: no cover - unreachable; makes this a generator function


def _cfg(**lenses) -> WhetstoneConfig:
    return WhetstoneConfig(
        project=ProjectConfig(name="demo"),
        lenses={name: LensConfig(**opts) for name, opts in lenses.items()},
    )


def test_run_persists_candidates_and_counts_new(tmp_path, monkeypatch):
    monkeypatch.setattr("whetstone.runner.resolve_files", lambda *a, **k: ())
    register(_Stub(count=2))
    conn = connect(tmp_path)
    result = execute_run(
        conn, _cfg(stub={}), tmp_path, tmp_path, tier="quick", changed_only=False
    )
    assert (result.new, result.seen) == (2, 0)
    assert len(list_findings(conn)) == 2


def test_rerun_counts_as_seen_not_new(tmp_path, monkeypatch):
    monkeypatch.setattr("whetstone.runner.resolve_files", lambda *a, **k: ())
    register(_Stub(count=1))
    conn = connect(tmp_path)
    cfg = _cfg(stub={})
    execute_run(conn, cfg, tmp_path, tmp_path, tier="quick", changed_only=False)
    second = execute_run(conn, cfg, tmp_path, tmp_path, tier="quick", changed_only=False)
    assert (second.new, second.seen) == (0, 1)


def test_disabled_lens_is_skipped_with_a_reason(tmp_path, monkeypatch):
    monkeypatch.setattr("whetstone.runner.resolve_files", lambda *a, **k: ())
    register(_Stub())
    conn = connect(tmp_path)
    result = execute_run(
        conn,
        _cfg(stub={"enabled": False}),
        tmp_path,
        tmp_path,
        tier="quick",
        changed_only=False,
    )
    assert any("disabled" in skip for skip in result.skips)
    assert result.new == 0


def test_unknown_lens_is_skipped_with_a_reason(tmp_path, monkeypatch):
    monkeypatch.setattr("whetstone.runner.resolve_files", lambda *a, **k: ())
    conn = connect(tmp_path)
    result = execute_run(
        conn, _cfg(nosuchlens={}), tmp_path, tmp_path, tier="quick", changed_only=False
    )
    assert any("not installed" in skip for skip in result.skips)


def test_tier_gating_is_reported(tmp_path, monkeypatch):
    monkeypatch.setattr("whetstone.runner.resolve_files", lambda *a, **k: ())
    register(_Stub(tiers=("deep",)))
    conn = connect(tmp_path)
    result = execute_run(
        conn, _cfg(stub={}), tmp_path, tmp_path, tier="quick", changed_only=False
    )
    assert any("tier 'quick'" in skip for skip in result.skips)


def test_lens_skips_are_propagated(tmp_path, monkeypatch):
    monkeypatch.setattr("whetstone.runner.resolve_files", lambda *a, **k: ())
    register(_Stub())
    conn = connect(tmp_path)
    result = execute_run(
        conn, _cfg(stub={}), tmp_path, tmp_path, tier="quick", changed_only=False
    )
    assert "stub: a deliberate skip" in result.skips


def test_unsupported_sink_is_reported_not_ignored(tmp_path, monkeypatch):
    """M0 implements only the built-in dashboard sink. Declaring another must
    not silently do nothing — the user would believe findings were published."""
    from whetstone.config.model import SinkConfig

    monkeypatch.setattr("whetstone.runner.resolve_files", lambda *a, **k: ())
    register(_Stub())
    conn = connect(tmp_path)
    cfg = _cfg(stub={})
    cfg.sinks = [SinkConfig(kind="dashboard"), SinkConfig(kind="jira")]
    result = execute_run(
        conn, cfg, tmp_path, tmp_path, tier="quick", changed_only=False
    )
    assert any("jira" in skip and "not implemented" in skip for skip in result.skips)
    assert not any("dashboard" in skip for skip in result.skips)


def test_run_row_is_recorded(tmp_path, monkeypatch):
    monkeypatch.setattr("whetstone.runner.resolve_files", lambda *a, **k: (Path("a"),))
    register(_Stub())
    conn = connect(tmp_path)
    result = execute_run(
        conn, _cfg(stub={}), tmp_path, tmp_path, tier="quick", changed_only=False
    )
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (result.run_id,)).fetchone()
    assert row["status"] == "complete"
    assert row["file_count"] == 1


def test_run_row_closed_when_lens_raises_immediately(tmp_path, monkeypatch):
    """A lens raising before it ever yields or skips must still close the
    `runs` row rather than leaving it stuck at status='running' forever."""
    monkeypatch.setattr("whetstone.runner.resolve_files", lambda *a, **k: ())
    register(_ImmediateBoom())
    conn = connect(tmp_path)
    cfg = _cfg(boom={})

    with pytest.raises(RuntimeError, match="boom immediately"):
        execute_run(conn, cfg, tmp_path, tmp_path, tier="quick", changed_only=False)

    row = conn.execute("SELECT * FROM runs").fetchone()
    assert row["status"] == "failed"
    assert row["finished_at"] is not None
    assert json.loads(row["skipped_json"]) == []


def test_skip_before_raise_is_not_lost(tmp_path, monkeypatch):
    """A skip recorded before a lens raises must still reach RunResult/the
    stored run, and any candidate already yielded must still persist. The
    run row closes as 'failed' and the exception still propagates -- this
    is not the quiet-clean-run failure the module exists to prevent -- but a
    failed run's own skip trail must not come back empty."""
    monkeypatch.setattr("whetstone.runner.resolve_files", lambda *a, **k: ())
    register(_PartialThenBoom())
    conn = connect(tmp_path)
    cfg = _cfg(partial={})

    with pytest.raises(RuntimeError, match="boom mid-lens"):
        execute_run(conn, cfg, tmp_path, tmp_path, tier="quick", changed_only=False)

    row = conn.execute("SELECT * FROM runs").fetchone()
    assert row["status"] == "failed"
    assert row["finished_at"] is not None
    skips = json.loads(row["skipped_json"])
    assert "partial: about to explode" in skips

    findings = list_findings(conn)
    assert len(findings) == 1
    assert findings[0].subject == "file0.py"


class _Interrupted:
    """Yields one candidate, then takes the BaseException exit that Ctrl-C
    takes. `except Exception` never sees this."""

    name = "interrupted"
    max_autonomy = 3

    def supports_tier(self, tier: str) -> bool:
        return True

    def run(self, ctx: RunContext):
        ctx.skip("interrupted: recorded before the interrupt")
        yield Candidate(
            lens="interrupted",
            rule_id="R0",
            subject="file0.py",
            title="t",
            detail="d",
            severity=Severity.low,
            evidence=Evidence(EvidenceKind.metric, "s", {}),
        )
        raise KeyboardInterrupt


class _SystemExited:
    name = "exited"
    max_autonomy = 3

    def supports_tier(self, tier: str) -> bool:
        return True

    def run(self, ctx: RunContext):
        raise SystemExit(1)
        yield  # pragma: no cover - unreachable; makes this a generator function


@pytest.mark.parametrize(
    "pack,exc",
    [(_Interrupted, KeyboardInterrupt), (_SystemExited, SystemExit)],
    ids=["keyboard-interrupt", "system-exit"],
)
def test_a_baseexception_exit_does_not_record_the_run_as_complete(
    tmp_path, monkeypatch, pack, exc
):
    """Ctrl-C mid-run must not close the row as if everything succeeded.

    `status` started at 'complete' and was downgraded by `except Exception`.
    KeyboardInterrupt, SystemExit and GeneratorExit are BaseException, so they
    walked past that clause into the `finally` and wrote status='complete' on a
    run holding only the findings recorded before the interrupt. The hygiene
    lens shells out to pip-audit, so an interrupt during a slow audit is the
    ordinary case. An incomplete run that reads as clean is the one outcome
    this module exists to prevent.
    """
    monkeypatch.setattr("whetstone.runner.resolve_files", lambda *a, **k: ())
    register(pack())
    conn = connect(tmp_path)
    cfg = _cfg(**{pack.name: {}})

    with pytest.raises(exc):
        execute_run(conn, cfg, tmp_path, tmp_path, tier="quick", changed_only=False)

    row = conn.execute("SELECT * FROM runs").fetchone()
    assert row["status"] == "failed"
    assert row["finished_at"] is not None


def test_an_interrupt_still_records_the_skips_taken_before_it(tmp_path, monkeypatch):
    """Downgrading the status must not cost the trail: what the run did manage
    to record before the interrupt is still the honest partial answer."""
    monkeypatch.setattr("whetstone.runner.resolve_files", lambda *a, **k: ())
    register(_Interrupted())
    conn = connect(tmp_path)

    with pytest.raises(KeyboardInterrupt):
        execute_run(
            conn, _cfg(interrupted={}), tmp_path, tmp_path, tier="quick",
            changed_only=False,
        )

    row = conn.execute("SELECT * FROM runs").fetchone()
    assert "interrupted: recorded before the interrupt" in json.loads(
        row["skipped_json"]
    )
    assert len(list_findings(conn)) == 1


def test_a_lens_clearing_its_own_skips_cannot_erase_another_lenss(tmp_path, monkeypatch):
    """A lens's private skip list must stay private. Sharing result.skips
    directly with every RunContext (an earlier fix for the raise-after-skip
    loss) let one misbehaving lens wipe every other lens's skip trail with
    ctx.skips.clear() and have the run report status='complete' anyway --
    worse than the bug it was meant to fix."""
    monkeypatch.setattr("whetstone.runner.resolve_files", lambda *a, **k: ())
    register(_Honest())
    register(_Evil())
    conn = connect(tmp_path)
    cfg = _cfg(honest={}, evil={})

    result = execute_run(
        conn, cfg, tmp_path, tmp_path, tier="quick", changed_only=False
    )

    assert "honest: legitimate skip" in result.skips
    assert "evil: pretending nothing happened before me" in result.skips

    row = conn.execute("SELECT * FROM runs WHERE id = ?", (result.run_id,)).fetchone()
    stored_skips = json.loads(row["skipped_json"])
    assert "honest: legitimate skip" in stored_skips


# --- finding 6: boundaries, lens scope, and lazy file resolution -------------


class _ProjectScoped:
    """Reads fixed artifacts, never ctx.files."""

    name = "wide"
    max_autonomy = 3
    scope = "project"

    def supports_tier(self, tier: str) -> bool:
        return True

    def run(self, ctx: RunContext):
        return
        yield  # pragma: no cover - unreachable; makes this a generator function


class _HighAndLow:
    """One candidate per severity, for the severity_floor tests."""

    name = "graded"
    max_autonomy = 3

    def supports_tier(self, tier: str) -> bool:
        return True

    def run(self, ctx: RunContext):
        for rule, severity in (("LOW", Severity.low), ("HIGH", Severity.high)):
            yield Candidate(
                lens="graded",
                rule_id=rule,
                subject=f"{rule}.py",
                title="t",
                detail="d",
                severity=severity,
                evidence=Evidence(EvidenceKind.metric, "s", {}),
            )


class _MisdeclaredScope:
    """Declares a scope that is not a LensScope. Typo, or a value from a
    future version of the enum."""

    name = "misdeclared"
    max_autonomy = 3
    scope = "projet"

    def supports_tier(self, tier: str) -> bool:
        return True

    def run(self, ctx: RunContext):
        return
        yield  # pragma: no cover - unreachable; makes this a generator function


def _boom_resolve(*args, **kwargs):
    raise AssertionError("resolve_files must not run for a project-scoped run")


def test_a_project_scoped_run_does_not_resolve_files(tmp_path, monkeypatch):
    """resolve_files gates the whole run, so a non-git project died before any
    lens started -- even though the only lens shipped today needs no files."""
    monkeypatch.setattr("whetstone.runner.resolve_files", _boom_resolve)
    register(_ProjectScoped())
    conn = connect(tmp_path)
    result = execute_run(
        conn, _cfg(wide={}), tmp_path, tmp_path, tier="quick", changed_only=False
    )
    assert result.file_count == 0
    assert any(
        "file resolution was not performed" in skip for skip in result.skips
    ), result.skips


def test_a_file_scoped_lens_still_resolves_files(tmp_path, monkeypatch):
    """The laziness must not become a way to skip resolution when it matters."""
    monkeypatch.setattr("whetstone.runner.resolve_files", lambda *a, **k: (Path("a"),))
    register(_Stub())
    conn = connect(tmp_path)
    result = execute_run(
        conn, _cfg(stub={}), tmp_path, tmp_path, tier="quick", changed_only=False
    )
    assert result.file_count == 1
    assert not any("file resolution" in skip for skip in result.skips)


def test_a_project_scoped_lens_reports_that_it_ignored_boundaries(
    tmp_path, monkeypatch
):
    """`exclude: ["coverage.xml"]` still produced a finding about coverage.xml
    and nothing said the pattern had no effect."""
    monkeypatch.setattr("whetstone.runner.resolve_files", _boom_resolve)
    register(_ProjectScoped())
    conn = connect(tmp_path)
    cfg = _cfg(wide={})
    cfg.boundaries.exclude = ["coverage.xml"]
    result = execute_run(
        conn, cfg, tmp_path, tmp_path, tier="quick", changed_only=False
    )
    assert any(
        "wide" in skip and "did NOT narrow" in skip for skip in result.skips
    ), result.skips


def test_an_invalid_scope_declaration_is_reported_not_silently_defaulted(
    tmp_path, monkeypatch
):
    """`scope = "projet"` and no `scope` at all both end at file-scoped, and
    they are not the same event. The pack that never declared one is behaving
    correctly; the pack with the typo has been overruled, its boundaries
    advisory is missing, and nothing said why."""
    monkeypatch.setattr("whetstone.runner.resolve_files", lambda *a, **k: ())
    register(_MisdeclaredScope())
    conn = connect(tmp_path)
    result = execute_run(
        conn, _cfg(misdeclared={}), tmp_path, tmp_path, tier="quick", changed_only=False
    )
    assert any(
        "misdeclared" in skip and "projet" in skip and "file-scoped" in skip
        for skip in result.skips
    ), result.skips
    # Reported once, not once per question the runner asks about the scope.
    assert sum("projet" in skip for skip in result.skips) == 1, result.skips


def test_a_pack_that_declares_no_scope_is_not_reported(tmp_path, monkeypatch):
    """The counterweight: silence for the packs written before `scope` existed,
    or the advisory becomes a line on every honest lens and nobody reads it."""
    monkeypatch.setattr("whetstone.runner.resolve_files", lambda *a, **k: ())
    register(_Stub())
    conn = connect(tmp_path)
    result = execute_run(
        conn, _cfg(stub={}), tmp_path, tmp_path, tier="quick", changed_only=False
    )
    assert not any("declared scope" in skip for skip in result.skips), result.skips


def test_default_boundaries_do_not_produce_a_noise_line(tmp_path, monkeypatch):
    """A skip list full of known-true, known-useless lines is one nobody
    reads."""
    monkeypatch.setattr("whetstone.runner.resolve_files", _boom_resolve)
    register(_ProjectScoped())
    conn = connect(tmp_path)
    result = execute_run(
        conn, _cfg(wide={}), tmp_path, tmp_path, tier="quick", changed_only=False
    )
    assert not any("did NOT narrow" in skip for skip in result.skips)


def test_a_disabled_project_scoped_lens_is_not_reported_as_ignoring_boundaries(
    tmp_path, monkeypatch
):
    monkeypatch.setattr("whetstone.runner.resolve_files", lambda *a, **k: ())
    register(_ProjectScoped())
    conn = connect(tmp_path)
    cfg = _cfg(wide={"enabled": False})
    cfg.boundaries.exclude = ["coverage.xml"]
    result = execute_run(
        conn, cfg, tmp_path, tmp_path, tier="quick", changed_only=False
    )
    assert not any("did NOT narrow" in skip for skip in result.skips)


# --- finding 5: severity_floor was validated and read by nothing -------------


def test_severity_floor_suppresses_and_says_so(tmp_path, monkeypatch):
    monkeypatch.setattr("whetstone.runner.resolve_files", lambda *a, **k: ())
    register(_HighAndLow())
    conn = connect(tmp_path)
    result = execute_run(
        conn,
        _cfg(graded={"severity_floor": "high"}),
        tmp_path,
        tmp_path,
        tier="quick",
        changed_only=False,
    )
    assert result.new == 1
    assert [f.rule_id for f in list_findings(conn)] == ["HIGH"]
    assert any(
        "severity_floor" in skip and "suppressed 1" in skip for skip in result.skips
    ), result.skips


def test_no_severity_floor_records_everything(tmp_path, monkeypatch):
    monkeypatch.setattr("whetstone.runner.resolve_files", lambda *a, **k: ())
    register(_HighAndLow())
    conn = connect(tmp_path)
    result = execute_run(
        conn, _cfg(graded={}), tmp_path, tmp_path, tier="quick", changed_only=False
    )
    assert result.new == 2
    assert not any("severity_floor" in skip for skip in result.skips)


# --- finding 5: the config path, end to end, with no hand-built RunContext ---


def test_a_configured_coverage_floor_reaches_the_detector(tmp_path):
    """Proof through the real path: YAML -> load_config -> execute_run ->
    detector -> stored finding. Nothing here builds a RunContext by hand.

    The floor used to be unreachable in production from both directions.
    LensConfig forbade the key, so writing it made the config fail to load;
    not writing it left the floor pinned at the 60 default. 70% coverage is
    above that default and below the 80 configured here, so a finding at all
    is only possible if the configured value arrived.
    """
    from whetstone.config.loader import load_config

    (tmp_path / "coverage.xml").write_text(
        '<?xml version="1.0"?><coverage line-rate="0.70" version="7.0"/>',
        encoding="utf-8",
    )
    (tmp_path / "whetstone.yaml").write_text(
        "version: 1\n"
        "project:\n"
        "  name: demo\n"
        "lenses:\n"
        "  hygiene:\n"
        "    only: [coverage]\n"
        "    options:\n"
        "      coverage_floor: 80\n",
        encoding="utf-8",
    )

    cfg = load_config(tmp_path / "whetstone.yaml")
    conn = connect(tmp_path)
    result = execute_run(
        conn, cfg, tmp_path, tmp_path, tier="quick", changed_only=False
    )

    assert result.new == 1
    findings = list_findings(conn)
    assert [f.rule_id for f in findings] == ["coverage-below-floor"]
    assert findings[0].evidence["data"]["floor"] == 80
    assert "80% floor" in findings[0].title


def test_without_the_option_the_default_floor_still_applies(tmp_path):
    """The other half: 70% is above the 60 default, so silence here is what
    proves the previous test measured the configured value and not a
    permanently-failing check."""
    from whetstone.config.loader import load_config

    (tmp_path / "coverage.xml").write_text(
        '<?xml version="1.0"?><coverage line-rate="0.70" version="7.0"/>',
        encoding="utf-8",
    )
    (tmp_path / "whetstone.yaml").write_text(
        "version: 1\n"
        "project:\n"
        "  name: demo\n"
        "lenses:\n"
        "  hygiene:\n"
        "    only: [coverage]\n",
        encoding="utf-8",
    )

    cfg = load_config(tmp_path / "whetstone.yaml")
    conn = connect(tmp_path)
    result = execute_run(
        conn, cfg, tmp_path, tmp_path, tier="quick", changed_only=False
    )
    assert result.new == 0
    assert list_findings(conn) == []
    # Silence only proves the default floor if the detector actually ran. A
    # missing coverage.xml, an `only` filter that stopped matching, and a
    # detector raising into HygienePack's guard all produce this same empty
    # result -- and each of the three records a `hygiene/coverage:` skip.
    assert not any(
        skip.startswith("hygiene/coverage") for skip in result.skips
    ), result.skips
