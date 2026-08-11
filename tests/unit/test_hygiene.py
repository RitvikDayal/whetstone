import json
from pathlib import Path

from whetstone.lenses.base import RunContext, Severity
from whetstone.lenses.hygiene.detectors.coverage import CoverageDetector
from whetstone.lenses.hygiene.detectors.deps import DepsDetector
from whetstone.lenses.hygiene.pack import HygienePack

COVERAGE_XML = """<?xml version="1.0"?>
<coverage line-rate="{rate}" version="7.0">
  <packages/>
</coverage>
"""

PIP_AUDIT_JSON = json.dumps(
    {
        "dependencies": [
            {
                "name": "requests",
                "version": "2.19.0",
                "vulns": [
                    {
                        "id": "GHSA-xxxx",
                        "fix_versions": ["2.31.0"],
                        "description": "Header injection.",
                    }
                ],
            },
            {"name": "click", "version": "8.1.7", "vulns": []},
        ]
    }
)


def _ctx(tmp_path: Path, **options) -> RunContext:
    return RunContext(
        project_root=tmp_path,
        state_root=tmp_path / "state",
        files=(),
        tier="quick",
        lens_options=options,
        run_id="run-1",
    )


def test_pack_identity():
    pack = HygienePack()
    assert pack.name == "hygiene"
    assert pack.max_autonomy == 3
    assert pack.supports_tier("quick")
    assert pack.supports_tier("deep")


def test_coverage_below_floor_is_a_finding(tmp_path):
    (tmp_path / "coverage.xml").write_text(
        COVERAGE_XML.format(rate="0.41"), encoding="utf-8"
    )
    ctx = _ctx(tmp_path, coverage_floor=60)
    found = list(CoverageDetector().detect(ctx))
    assert len(found) == 1
    assert found[0].rule_id == "coverage-below-floor"
    assert found[0].evidence.data["measured"] == 41.0
    assert found[0].evidence.data["floor"] == 60


def test_coverage_at_or_above_floor_is_silent(tmp_path):
    (tmp_path / "coverage.xml").write_text(
        COVERAGE_XML.format(rate="0.72"), encoding="utf-8"
    )
    assert list(CoverageDetector().detect(_ctx(tmp_path, coverage_floor=60))) == []


def test_missing_coverage_artifact_skips_loudly(tmp_path):
    ctx = _ctx(tmp_path, coverage_floor=60)
    assert list(CoverageDetector().detect(ctx)) == []
    assert any("coverage" in skip for skip in ctx.skips)


def test_deps_parses_advisories(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "whetstone.lenses.hygiene.detectors.deps._run_pip_audit",
        lambda root: PIP_AUDIT_JSON,
    )
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    found = list(DepsDetector().detect(_ctx(tmp_path)))
    assert len(found) == 1
    assert found[0].subject == "requests"
    assert found[0].rule_id == "GHSA-xxxx"
    assert found[0].severity is Severity.high
    assert "2.31.0" in found[0].detail


def test_deps_skips_loudly_when_tool_absent(tmp_path, monkeypatch):
    def _boom(root):
        raise FileNotFoundError("pip-audit")

    monkeypatch.setattr(
        "whetstone.lenses.hygiene.detectors.deps._run_pip_audit", _boom
    )
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    ctx = _ctx(tmp_path)
    assert list(DepsDetector().detect(ctx)) == []
    assert any("pip-audit" in skip for skip in ctx.skips)


def test_deps_skips_when_no_python_manifest(tmp_path):
    ctx = _ctx(tmp_path)
    assert list(DepsDetector().detect(ctx)) == []
    assert any("no Python manifest" in skip for skip in ctx.skips)


def test_only_option_restricts_detectors(tmp_path):
    ctx = _ctx(tmp_path, only=["coverage"])
    list(HygienePack().run(ctx))
    assert any("deps" in skip and "not in `only`" in skip for skip in ctx.skips)
