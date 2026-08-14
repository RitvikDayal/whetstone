import json
from pathlib import Path

import pytest
from pydantic import SecretStr

import whetstone.lenses.registry as registry_module
from whetstone.config.model import LensConfig, ProjectConfig, WhetstoneConfig
from whetstone.errors import LensError, WhetstoneError
from whetstone.lenses.base import (
    Candidate,
    Evidence,
    EvidenceKind,
    LensRuntime,
    RunContext,
    Severity,
)
from whetstone.lenses.registry import register
from whetstone.runner import execute_run, get_last_run
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
    """Tries every route to erase a skip trail before recording its own.

    A lens's blast radius must stop at its own trail -- lens packs become
    third-party code once the plugin API is public, and 'a lens would not do
    that' is not a defence. Since issue #15 all four routes raise, so this also
    proves the barrier is real and not just the per-lens RunContext behind it:
    the attempts are swallowed here on purpose, because the point of the test is
    what the OTHER lens's trail looks like afterwards.
    """

    name = "evil"
    max_autonomy = 3
    attempts_refused = 0

    def supports_tier(self, tier: str) -> bool:
        return True

    def run(self, ctx: RunContext):
        for attempt in (
            lambda: ctx.skips.clear(),
            lambda: ctx.skips.append("forged"),
            lambda: ctx.skips.__setitem__(slice(None), []),
            lambda: setattr(ctx, "skips", []),
        ):
            try:
                attempt()
            except (AttributeError, TypeError):
                type(self).attempts_refused += 1
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
    _Evil.attempts_refused = 0
    register(_Evil())
    conn = connect(tmp_path)
    cfg = _cfg(honest={}, evil={})

    result = execute_run(
        conn, cfg, tmp_path, tmp_path, tier="quick", changed_only=False
    )

    # The population guard: `attempts_refused == 4` says the evil lens actually
    # ran and tried all four routes. Without it, a lens that never executed
    # satisfies every assertion below by doing nothing.
    assert _Evil.attempts_refused == 4, _Evil.attempts_refused
    assert "honest: legitimate skip" in result.skips
    assert "evil: pretending nothing happened before me" in result.skips
    assert "forged" not in result.skips

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


# --- a run in which no lens ran is the loudest result, not the quietest -----


def test_a_config_declaring_no_lenses_says_so_instead_of_reading_as_clean(
    tmp_path, monkeypatch
):
    """`lenses` defaults to an empty dict (config/model.py), and the only skip
    line in the no-file-scoped branch was guarded by `if plan:` -- suppressed
    exactly when nothing ran.

    Measured before the fix, against a real project pinning requests==2.19.0
    at 5% line coverage: `run` printed "0 new, 0 already known - tier quick -
    0 files in scope" with no skip lines, `findings` printed "No findings in
    state 'queued'.", and `report` wrote "No open findings." -- all exit 0.
    Declaring `lenses: {hygiene: {enabled: true}}` in that same directory
    produced 24 findings including PYSEC-2018-28.
    """
    monkeypatch.setattr("whetstone.runner.resolve_files", lambda *a, **k: ())
    conn = connect(tmp_path)
    result = execute_run(
        conn, _cfg(), tmp_path, tmp_path, tier="quick", changed_only=False
    )

    assert result.lens_count == 0
    assert any("NO LENS RAN" in skip for skip in result.skips), result.skips
    assert any("no lenses at all" in skip for skip in result.skips), result.skips
    # Stored, not just returned: `report` reads the row, not this object.
    stored = json.loads(
        conn.execute(
            "SELECT skipped_json FROM runs WHERE id = ?", (result.run_id,)
        ).fetchone()["skipped_json"]
    )
    assert any("NO LENS RAN" in skip for skip in stored), stored


def test_every_lens_being_skipped_also_reports_that_nothing_ran(tmp_path, monkeypatch):
    """The other route to an empty plan. The per-lens reasons are there, but
    none of them says the SUM was zero, and 'disabled in config' reads as one
    lens opting out rather than as the whole run examining nothing."""
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

    assert result.lens_count == 0
    assert any("NO LENS RAN" in skip for skip in result.skips), result.skips
    # Worded for the cause it actually had, not the one it did not.
    assert not any("no lenses at all" in skip for skip in result.skips), result.skips
    assert any("disabled in config" in skip for skip in result.skips), result.skips


def test_a_run_that_did_examine_something_does_not_claim_nothing_ran(
    tmp_path, monkeypatch
):
    """The counterweight: an always-on 'nothing ran' line would be noise."""
    monkeypatch.setattr("whetstone.runner.resolve_files", lambda *a, **k: ())
    register(_Stub())
    conn = connect(tmp_path)
    result = execute_run(
        conn, _cfg(stub={}), tmp_path, tmp_path, tier="quick", changed_only=False
    )
    assert result.lens_count == 1
    assert not any("NO LENS RAN" in skip for skip in result.skips), result.skips


# --- get_last_run: report --out needs a run's skips, not just its findings ---


def test_get_last_run_returns_none_when_no_run_has_happened(tmp_path):
    """A store with no run row at all is a distinct state from a run with no
    skips -- the caller must be able to tell them apart."""
    conn = connect(tmp_path)
    assert get_last_run(conn) is None


def test_get_last_run_carries_the_stored_skips(tmp_path, monkeypatch):
    monkeypatch.setattr("whetstone.runner.resolve_files", lambda *a, **k: ())
    conn = connect(tmp_path)
    execute_run(
        conn, _cfg(nosuchlens={}), tmp_path, tmp_path, tier="quick", changed_only=False
    )
    last = get_last_run(conn)
    assert last is not None
    assert any("not installed" in skip for skip in last.skips)


def test_get_last_run_derives_new_and_seen_from_the_findings_table(
    tmp_path, monkeypatch
):
    """`runs` has no new/seen columns; fabricating them as 0 would be exactly
    the kind of dishonest silence this project exists to forbid, so they are
    derived from `findings.first_seen_run`/`last_seen_run` against the run's
    own id -- the same evidence `execute_run` itself counted live."""
    monkeypatch.setattr("whetstone.runner.resolve_files", lambda *a, **k: ())
    register(_Stub(count=2))
    conn = connect(tmp_path)

    execute_run(conn, _cfg(stub={}), tmp_path, tmp_path, tier="quick", changed_only=False)
    first = get_last_run(conn)
    assert (first.new, first.seen) == (2, 0)

    execute_run(conn, _cfg(stub={}), tmp_path, tmp_path, tier="quick", changed_only=False)
    second = get_last_run(conn)
    assert (second.new, second.seen) == (0, 2)


def test_get_last_run_prefers_a_later_failed_run_over_an_earlier_complete_one(
    tmp_path, monkeypatch
):
    """Ordered by started_at, not finished_at or status: a run that failed
    mid-way is exactly the one whose skips matter most to a reader of a
    report generated afterward, so excluding failed runs from "most recent"
    would hide the run most worth surfacing."""
    monkeypatch.setattr("whetstone.runner.resolve_files", lambda *a, **k: ())
    register(_Stub())
    register(_ImmediateBoom())
    conn = connect(tmp_path)

    execute_run(conn, _cfg(stub={}), tmp_path, tmp_path, tier="quick", changed_only=False)
    with pytest.raises(RuntimeError):
        execute_run(
            conn, _cfg(boom={}), tmp_path, tmp_path, tier="quick", changed_only=False
        )

    failed_row = conn.execute("SELECT id FROM runs WHERE status = 'failed'").fetchone()
    last = get_last_run(conn)
    assert last is not None
    assert last.run_id == failed_row["id"]


def test_get_last_run_carries_the_status_it_selected(tmp_path, monkeypatch):
    """`SELECT *` read tier, file_count and skipped_json out of the row and
    left `status` behind, and `RunResult` had nowhere to put it -- so the one
    column that says whether the run finished could not reach the report.

    An interrupt records no skip at all, so the skip list cannot substitute:
    this is the only evidence that exists in the failure case the ordering
    rule above was written for.
    """
    monkeypatch.setattr("whetstone.runner.resolve_files", lambda *a, **k: ())
    register(_Stub())
    conn = connect(tmp_path)

    execute_run(conn, _cfg(stub={}), tmp_path, tmp_path, tier="quick", changed_only=False)
    assert get_last_run(conn).status == "complete"
    assert get_last_run(conn).finished is True

    register(_Interrupted())
    with pytest.raises(KeyboardInterrupt):
        execute_run(
            conn, _cfg(interrupted={}), tmp_path, tmp_path, tier="quick",
            changed_only=False,
        )

    last = get_last_run(conn)
    assert last.status == "failed"
    assert last.finished is False


def test_get_last_run_leaves_lens_count_unknown_rather_than_claiming_zero(
    tmp_path, monkeypatch
):
    """`runs` has no lens-count column. Defaulting to 0 on a reconstructed run
    would assert that a real run examined nothing -- the same fabricated
    silence `new`/`seen` are derived to avoid."""
    monkeypatch.setattr("whetstone.runner.resolve_files", lambda *a, **k: ())
    register(_Stub())
    conn = connect(tmp_path)
    live = execute_run(
        conn, _cfg(stub={}), tmp_path, tmp_path, tier="quick", changed_only=False
    )
    assert live.lens_count == 1
    assert get_last_run(conn).lens_count is None


# --- unstorable text must not escape as a bare traceback ---------------------
#
# A lone surrogate is a perfectly ordinary `str`: it passes `isinstance(v, str)`
# and `.strip()`, and then dies at the sqlite boundary with a
# `UnicodeEncodeError`, which is NOT a `WhetstoneError` -- so it sails past the
# CLI's `except WhetstoneError` and reaches the user as a bare traceback, from
# inside a transaction whose `runs` row already exists.
#
# Tested through `execute_run` rather than against `Candidate` alone, because
# the store call that actually breaks is in the runner. A constructor-only test
# is structurally unable to see whether the run survives.

_LONE_SURROGATE_SUBJECT = "src/caf\udce9.py"


class _ModelShaped:
    """A lens shaped like M1a's: its field values came from model output, so a
    surrogate arrives by the ordinary route rather than an exotic one."""

    name = "modelshaped"
    max_autonomy = 3

    def __init__(self, **overrides):
        self._overrides = overrides

    def supports_tier(self, tier: str) -> bool:
        return True

    def run(self, ctx: RunContext):
        fields = dict(
            lens="modelshaped",
            rule_id="R1",
            subject="src/ok.py",
            title="t",
            detail="d",
            severity=Severity.low,
            evidence=Evidence(EvidenceKind.metric, "s", {"k": 1}),
        )
        fields.update(self._overrides)
        yield Candidate(**fields)


def _run_with(tmp_path, monkeypatch, pack):
    monkeypatch.setattr("whetstone.runner.resolve_files", lambda *a, **k: ())
    register(pack)
    conn = connect(tmp_path)
    return execute_run(
        conn,
        _cfg(**{pack.name: {}}),
        tmp_path,
        tmp_path,
        tier="quick",
        changed_only=False,
    )


@pytest.mark.parametrize("field", ["subject", "rule_id", "title", "detail", "lens"])
def test_a_surrogate_in_any_text_field_fails_as_a_whetstone_error(
    tmp_path, monkeypatch, field
):
    """Not `pytest.raises(LensError)` alone -- the assertion that matters is
    that it is NOT a UnicodeEncodeError, because that is the class the CLI does
    not catch."""
    monkeypatch.setattr("whetstone.runner.resolve_files", lambda *a, **k: ())
    register(_ModelShaped(**{field: _LONE_SURROGATE_SUBJECT}))
    conn = connect(tmp_path)
    with pytest.raises(WhetstoneError) as caught:
        execute_run(
            conn,
            _cfg(**{"modelshaped": {}}),
            tmp_path,
            tmp_path,
            tier="quick",
            changed_only=False,
        )
    assert not isinstance(caught.value, UnicodeEncodeError)
    assert field in str(caught.value)


def test_a_surrogate_inside_evidence_data_fails_as_a_whetstone_error(tmp_path):
    """`json.dumps` serialises a surrogate happily; the failure only appears
    when the resulting string is encoded on its way into sqlite."""
    with pytest.raises(LensError, match="data"):
        Evidence(EvidenceKind.metric, "s", {"path": _LONE_SURROGATE_SUBJECT})
    with pytest.raises(LensError, match="data"):
        Evidence(EvidenceKind.metric, "s", {_LONE_SURROGATE_SUBJECT: "v"})


def test_evidence_data_refuses_nan_which_no_other_json_parser_accepts(tmp_path):
    with pytest.raises(LensError, match="data"):
        Evidence(EvidenceKind.metric, "s", {"ratio": float("nan")})


def test_a_clean_model_shaped_candidate_still_runs(tmp_path, monkeypatch):
    """The population guard for the block above. Every test here asserts a
    refusal; without this one, a `Candidate` that refused everything would
    satisfy all of them."""
    result = _run_with(tmp_path, monkeypatch, _ModelShaped())
    assert result.status == "complete"
    assert result.new == 1
    assert [f.subject for f in list_findings(connect(tmp_path))] == ["src/ok.py"]


# --- the configure hook ---------------------------------------------------------
#
# A lens needing the run's config -- the declared test command, the cost ceiling
# -- gets it from the runner and nowhere else. RunContext deliberately does not
# carry the config, so without this hook a model-driven pack would have to
# re-load whetstone.yaml for itself.


class _NeedsConfig:
    """Returns a CONFIGURED COPY, the way the code-defects pack does."""

    name = "needsconfig"
    max_autonomy = 3

    def __init__(self, test_command: str | None = None):
        self.test_command = test_command

    def configure(self, runtime) -> "_NeedsConfig":
        return type(self)(runtime.test_command)

    def supports_tier(self, tier: str) -> bool:
        return True

    def run(self, ctx: RunContext):
        ctx.skip(f"needsconfig: test command is {self.test_command!r}")
        return
        yield  # pragma: no cover - unreachable; makes this a generator function


class _ConfigureRaises:
    name = "configureboom"
    max_autonomy = 3

    def configure(self, cfg):
        raise LensError("the provider it needs is not installed")

    def supports_tier(self, tier: str) -> bool:
        return True

    def run(self, ctx: RunContext):  # pragma: no cover - must never be reached
        raise AssertionError("a pack that could not be configured must not run")
        yield


def _cfg_with_test_command(command: str) -> WhetstoneConfig:
    return WhetstoneConfig(
        project=ProjectConfig(name="demo"),
        environment={"commands": {"test": command}},
        lenses={"needsconfig": LensConfig()},
    )


def test_a_pack_declaring_configure_is_given_the_runs_config(tmp_path, monkeypatch):
    monkeypatch.setattr("whetstone.runner.resolve_files", lambda *a, **k: ())
    register(_NeedsConfig())
    conn = connect(tmp_path)
    result = execute_run(
        conn,
        _cfg_with_test_command("pytest -q"),
        tmp_path,
        tmp_path,
        tier="deep",
        changed_only=False,
    )
    assert any("'pytest -q'" in skip for skip in result.skips), result.skips


class _RecordsWhatItWasGiven:
    name = "recordsconfig"
    max_autonomy = 3
    given: object = None

    def configure(self, runtime):
        type(self).given = runtime
        return self

    def supports_tier(self, tier: str) -> bool:
        return True

    def run(self, ctx: RunContext):
        return
        yield  # pragma: no cover - unreachable; makes this a generator function


def test_the_hook_is_handed_a_narrowed_record_and_not_the_config(tmp_path, monkeypatch):
    """`WhetstoneConfig.state_dir` is a `SecretStr` the loader has ALREADY
    resolved from `${env:...}`, so a pack handed the config object is one
    `get_secret_value()` from a project's credential -- and a lens pack arrives
    through an entry point. Asserted on the object the hook actually received,
    because `_lens_runtime` being correct proves nothing if the runner still
    passes `cfg`."""
    monkeypatch.setattr("whetstone.runner.resolve_files", lambda *a, **k: ())
    register(_RecordsWhatItWasGiven())
    cfg = _cfg_with_test_command("pytest -q")
    cfg = cfg.model_copy(update={"state_dir": SecretStr("s3cret-token-path")})
    cfg.lenses["recordsconfig"] = LensConfig()
    conn = connect(tmp_path)
    execute_run(conn, cfg, tmp_path, tmp_path, tier="deep", changed_only=False)

    given = _RecordsWhatItWasGiven.given
    assert isinstance(given, LensRuntime)
    assert not isinstance(given, WhetstoneConfig)
    assert not hasattr(given, "state_dir")
    assert "s3cret" not in repr(given)
    # Narrowed, not emptied: the thing the hook exists for still arrives.
    assert given.test_command == "pytest -q"


def test_configuring_does_not_edit_the_registered_pack(tmp_path, monkeypatch):
    """The registry hands out one instance for the life of the process, so a
    hook that configured in place would leak one project's settings into the
    next run."""
    monkeypatch.setattr("whetstone.runner.resolve_files", lambda *a, **k: ())
    registered = _NeedsConfig()
    register(registered)
    conn = connect(tmp_path)
    execute_run(
        conn,
        _cfg_with_test_command("pytest -q"),
        tmp_path,
        tmp_path,
        tier="deep",
        changed_only=False,
    )
    assert registered.test_command is None


def test_a_pack_that_cannot_be_configured_is_skipped_not_run(tmp_path, monkeypatch):
    monkeypatch.setattr("whetstone.runner.resolve_files", lambda *a, **k: ())
    register(_ConfigureRaises())
    conn = connect(tmp_path)
    result = execute_run(
        conn,
        _cfg(configureboom={}),
        tmp_path,
        tmp_path,
        tier="deep",
        changed_only=False,
    )
    # `could not be configured` is the configure branch's OWN wording. The
    # raised message ends in "not installed", which is also how the runner
    # reports a lens with no registered pack -- so matching that substring
    # alone passes with the whole configure block deleted, and with
    # `register()` never having taken effect. It named a behaviour it did not
    # pin.
    assert any(
        "could not be configured" in skip and "not installed" in skip
        for skip in result.skips
    ), result.skips
    assert result.lens_count == 0


class _ConfigureReturnsNone:
    """The in-place spelling `configure` invites: it sets an attribute and
    returns None, which is what every function without a `return` does."""

    name = "configurenone"
    max_autonomy = 3
    test_command: str | None = None

    def configure(self, runtime) -> None:
        self.test_command = runtime.test_command

    def supports_tier(self, tier: str) -> bool:
        return True

    def run(self, ctx: RunContext):  # pragma: no cover - must never be reached
        raise AssertionError("a pack whose configure returned None must not run")
        yield


class _ConfigureReturnsRubbish:
    name = "configurerubbish"
    max_autonomy = 3

    def configure(self, runtime) -> object:
        return "a lens pack, honest"

    def supports_tier(self, tier: str) -> bool:
        return True

    def run(self, ctx: RunContext):  # pragma: no cover - must never be reached
        raise AssertionError("a pack whose configure returned a str must not run")
        yield


@pytest.mark.parametrize(
    ("pack", "lens", "returned"),
    [
        (_ConfigureReturnsNone(), "configurenone", "NoneType"),
        (_ConfigureReturnsRubbish(), "configurerubbish", "str"),
    ],
    ids=["none", "not-a-pack"],
)
def test_a_configure_hook_returning_a_non_pack_skips_that_lens_alone(
    pack, lens, returned, tmp_path, monkeypatch
):
    """One third-party pack must not take the run with it.

    The return value was assigned unchecked, so `configure` returning None put
    None into `lens_scope_declaration` and raised `AttributeError` -- not a
    `WhetstoneError`, so it escaped `execute_run` BEFORE the `runs` INSERT. No
    run row, every other lens abandoned, and nothing anywhere saying why. The
    honest pack registered alongside it is what proves the blast radius is one
    lens rather than the run.
    """
    monkeypatch.setattr("whetstone.runner.resolve_files", lambda *a, **k: ())
    register(pack)
    register(_Stub())
    conn = connect(tmp_path)

    result = execute_run(
        conn,
        _cfg(**{lens: {}, "stub": {}}),
        tmp_path,
        tmp_path,
        tier="quick",
        changed_only=False,
    )

    assert result.status == "complete"
    assert any(
        lens in skip and returned in skip and "NOT run" in skip
        for skip in result.skips
    ), result.skips
    # The other lens ran to completion, which is the whole point of the skip.
    assert result.new == 1
    assert result.lens_count == 1


def test_a_pack_without_configure_still_runs(tmp_path, monkeypatch):
    """The population guard: the hook is optional, and a pack written before it
    existed -- including every installed third-party one -- must be untouched."""
    monkeypatch.setattr("whetstone.runner.resolve_files", lambda *a, **k: ())
    register(_Stub())
    conn = connect(tmp_path)
    result = execute_run(
        conn, _cfg(stub={}), tmp_path, tmp_path, tier="quick", changed_only=False
    )
    assert result.new == 1
