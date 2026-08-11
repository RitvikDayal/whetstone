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
