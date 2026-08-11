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


def _ctx(tmp_path: Path, *, only=None, **options) -> RunContext:
    """Build a context the way the runner does.

    Pack options go under `options`, not at the top level of `lens_options`:
    that is the shape `LensConfig` produces and the shape `RunContext.options`
    reads. `only` is a spine key and stays at the top level.
    """
    lens_options: dict = {"options": options}
    if only is not None:
        lens_options["only"] = only
    return RunContext(
        project_root=tmp_path,
        state_root=tmp_path / "state",
        files=(),
        tier="quick",
        lens_options=lens_options,
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
        lambda root, args: PIP_AUDIT_JSON,
    )
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    found = list(DepsDetector().detect(_ctx(tmp_path)))
    assert len(found) == 1
    assert found[0].subject == "requests"
    assert found[0].rule_id == "GHSA-xxxx"
    assert found[0].severity is Severity.high
    assert "2.31.0" in found[0].detail


def test_deps_skips_loudly_when_tool_absent(tmp_path, monkeypatch):
    def _boom(root, args):
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


def test_coverage_floor_non_numeric_skips_loudly(tmp_path):
    (tmp_path / "coverage.xml").write_text(
        COVERAGE_XML.format(rate="0.72"), encoding="utf-8"
    )
    ctx = _ctx(tmp_path, coverage_floor="not-a-number")
    assert list(CoverageDetector().detect(ctx)) == []
    assert any(
        "coverage_floor" in skip and "not-a-number" in skip for skip in ctx.skips
    )


def test_coverage_floor_float_does_not_silently_loosen(tmp_path):
    # 59.5% measured against a 59.9% floor must still be a finding. Truncating
    # the floor via int(59.9) == 59 would let this pass unreported -- a floor
    # that silently loosens is the wrong failure direction.
    (tmp_path / "coverage.xml").write_text(
        COVERAGE_XML.format(rate="0.595"), encoding="utf-8"
    )
    ctx = _ctx(tmp_path, coverage_floor=59.9)
    found = list(CoverageDetector().detect(ctx))
    assert len(found) == 1
    assert found[0].evidence.data["floor"] == 59.9


def test_coverage_floor_bool_true_is_rejected(tmp_path):
    # isinstance(True, int) is True and float(True) == 1.0 -- without an
    # explicit bool check, `coverage_floor: true` would silently become a
    # 1% floor instead of the misconfiguration it almost certainly is.
    (tmp_path / "coverage.xml").write_text(
        COVERAGE_XML.format(rate="0.72"), encoding="utf-8"
    )
    ctx = _ctx(tmp_path, coverage_floor=True)
    assert list(CoverageDetector().detect(ctx)) == []
    assert any("coverage_floor" in skip and "bool" in skip for skip in ctx.skips)


def test_coverage_floor_negative_is_rejected(tmp_path):
    (tmp_path / "coverage.xml").write_text(
        COVERAGE_XML.format(rate="0.72"), encoding="utf-8"
    )
    ctx = _ctx(tmp_path, coverage_floor=-5)
    assert list(CoverageDetector().detect(ctx)) == []
    assert any("coverage_floor" in skip and "-5" in skip for skip in ctx.skips)


def test_coverage_floor_zero_is_rejected(tmp_path):
    # A floor of 0 can never fail -- it isn't a floor, it's the check turned
    # off, and it looks identical to a clean project.
    (tmp_path / "coverage.xml").write_text(
        COVERAGE_XML.format(rate="0.001"), encoding="utf-8"
    )
    ctx = _ctx(tmp_path, coverage_floor=0)
    assert list(CoverageDetector().detect(ctx)) == []
    # `"0" in skip` also matched the 0 inside "100" in the same sentence, so
    # the assertion passed on any reported value, including a wrong one. The
    # siblings use -5 and 150, which are distinctive; only 0 needed delimiting.
    assert any(
        "coverage_floor" in skip and "out of range (0)" in skip for skip in ctx.skips
    ), ctx.skips


def test_coverage_floor_over_100_is_rejected(tmp_path):
    (tmp_path / "coverage.xml").write_text(
        COVERAGE_XML.format(rate="0.99"), encoding="utf-8"
    )
    ctx = _ctx(tmp_path, coverage_floor=150)
    assert list(CoverageDetector().detect(ctx)) == []
    assert any("coverage_floor" in skip and "150" in skip for skip in ctx.skips)


def test_coverage_floor_of_100_is_accepted(tmp_path):
    # 100 is a legitimate "require full coverage" value, unlike 0 which can
    # never fail. Only the *range* is invalid outside 0 < floor <= 100.
    (tmp_path / "coverage.xml").write_text(
        COVERAGE_XML.format(rate="0.99"), encoding="utf-8"
    )
    ctx = _ctx(tmp_path, coverage_floor=100)
    found = list(CoverageDetector().detect(ctx))
    assert len(found) == 1
    assert found[0].evidence.data["floor"] == 100.0
    assert not any("coverage_floor" in skip for skip in ctx.skips)


def test_coverage_floor_fractional_half_percent_is_accepted(tmp_path):
    (tmp_path / "coverage.xml").write_text(
        COVERAGE_XML.format(rate="0.001"), encoding="utf-8"
    )
    ctx = _ctx(tmp_path, coverage_floor=0.5)
    found = list(CoverageDetector().detect(ctx))
    assert len(found) == 1
    assert found[0].evidence.data["floor"] == 0.5
    assert not any("coverage_floor" in skip for skip in ctx.skips)


def test_coverage_floor_numeric_string_is_accepted(tmp_path):
    (tmp_path / "coverage.xml").write_text(
        COVERAGE_XML.format(rate="0.41"), encoding="utf-8"
    )
    ctx = _ctx(tmp_path, coverage_floor="60")
    found = list(CoverageDetector().detect(ctx))
    assert len(found) == 1
    assert found[0].evidence.data["floor"] == 60.0


def _deps_ctx(tmp_path: Path, payload: str, monkeypatch, **options) -> RunContext:
    monkeypatch.setattr(
        "whetstone.lenses.hygiene.detectors.deps._run_pip_audit",
        lambda root, args: payload,
    )
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    return _ctx(tmp_path, **options)


def test_deps_skips_when_pip_audit_returns_a_list(tmp_path, monkeypatch):
    ctx = _deps_ctx(tmp_path, "[]", monkeypatch)
    assert list(DepsDetector().detect(ctx)) == []
    assert any("pip-audit" in skip and "list" in skip for skip in ctx.skips)


def test_deps_skips_when_pip_audit_returns_null(tmp_path, monkeypatch):
    ctx = _deps_ctx(tmp_path, "null", monkeypatch)
    assert list(DepsDetector().detect(ctx)) == []
    assert any("pip-audit" in skip and "NoneType" in skip for skip in ctx.skips)


def test_deps_skips_when_dependencies_field_is_not_a_list(tmp_path, monkeypatch):
    payload = json.dumps({"dependencies": {"oops": "not-a-list"}})
    ctx = _deps_ctx(tmp_path, payload, monkeypatch)
    assert list(DepsDetector().detect(ctx)) == []
    assert any(
        "dependencies" in skip and "not a list" in skip for skip in ctx.skips
    )


def test_deps_skips_dependency_with_null_vulns(tmp_path, monkeypatch):
    payload = json.dumps(
        {
            "dependencies": [
                {"name": "foo", "version": "1.0", "vulns": None},
                {
                    "name": "bar",
                    "version": "2.0",
                    "vulns": [
                        {"id": "GHSA-yyyy", "fix_versions": [], "description": "d"}
                    ],
                },
            ]
        }
    )
    ctx = _deps_ctx(tmp_path, payload, monkeypatch)
    found = list(DepsDetector().detect(ctx))
    assert len(found) == 1
    assert found[0].subject == "bar"
    assert any("foo" in skip and "not a list" in skip for skip in ctx.skips)


def test_deps_skips_non_dict_dependency_entry(tmp_path, monkeypatch):
    payload = json.dumps(
        {
            "dependencies": [
                "not-an-object",
                {
                    "name": "bar",
                    "version": "2.0",
                    "vulns": [
                        {"id": "GHSA-yyyy", "fix_versions": [], "description": "d"}
                    ],
                },
            ]
        }
    )
    ctx = _deps_ctx(tmp_path, payload, monkeypatch)
    found = list(DepsDetector().detect(ctx))
    assert len(found) == 1
    assert found[0].subject == "bar"
    assert any("dependency entry" in skip for skip in ctx.skips)


def test_deps_skips_non_dict_vuln_entry(tmp_path, monkeypatch):
    payload = json.dumps(
        {
            "dependencies": [
                {
                    "name": "foo",
                    "version": "1.0",
                    "vulns": [
                        "not-an-object",
                        {"id": "GHSA-zzzz", "fix_versions": [], "description": "d"},
                    ],
                }
            ]
        }
    )
    ctx = _deps_ctx(tmp_path, payload, monkeypatch)
    found = list(DepsDetector().detect(ctx))
    assert len(found) == 1
    assert found[0].rule_id == "GHSA-zzzz"
    assert any("foo" in skip and "vuln" in skip.lower() for skip in ctx.skips)


# --- finding 4: one detector raising must not suppress the other -------------


class _ExplodingDetector:
    id = "deps"

    def detect(self, ctx):
        raise TypeError("sequence item 0: expected str instance, int found")
        yield  # pragma: no cover - unreachable; makes this a generator function


def test_one_detector_raising_does_not_suppress_the_other(tmp_path, monkeypatch):
    """`yield from detector.detect(ctx)` had no per-detector guard, so `deps`
    dying took `coverage` with it: the coverage finding vanished and nothing
    recorded that coverage never ran."""
    monkeypatch.setattr(
        "whetstone.lenses.hygiene.pack.DETECTORS",
        (_ExplodingDetector(), CoverageDetector()),
    )
    (tmp_path / "coverage.xml").write_text(
        COVERAGE_XML.format(rate="0.05"), encoding="utf-8"
    )
    ctx = _ctx(tmp_path, coverage_floor=60)

    found = list(HygienePack().run(ctx))

    assert [c.rule_id for c in found] == ["coverage-below-floor"]
    assert any(
        "deps" in skip and "TypeError" in skip and "expected str instance" in skip
        for skip in ctx.skips
    ), ctx.skips


def test_a_raising_detector_names_its_exception(tmp_path, monkeypatch):
    """A guard broad enough to hide a bug in our own code is the next defect.
    The type and message have to survive into the skip text."""
    monkeypatch.setattr(
        "whetstone.lenses.hygiene.pack.DETECTORS", (_ExplodingDetector(),)
    )
    ctx = _ctx(tmp_path)
    assert list(HygienePack().run(ctx)) == []
    assert len(ctx.skips) == 1
    assert "TypeError" in ctx.skips[0]


def test_hygiene_declares_itself_project_scoped():
    """Neither detector reads ctx.files, so boundaries do not narrow it. The
    runner can only tell the user that if the pack says so."""
    from whetstone.lenses.base import LensScope, lens_scope

    assert lens_scope(HygienePack()) is LensScope.project


# --- finding 8: the unreadable-artifact path had no test --------------------


def test_unreadable_coverage_xml_skips_loudly(tmp_path):
    (tmp_path / "coverage.xml").write_text("<coverage", encoding="utf-8")
    ctx = _ctx(tmp_path, coverage_floor=60)
    assert list(CoverageDetector().detect(ctx)) == []
    assert any("unreadable" in skip for skip in ctx.skips), ctx.skips


def test_coverage_xml_without_a_line_rate_skips_loudly(tmp_path):
    (tmp_path / "coverage.xml").write_text(
        '<?xml version="1.0"?><coverage version="7.0"/>', encoding="utf-8"
    )
    ctx = _ctx(tmp_path, coverage_floor=60)
    assert list(CoverageDetector().detect(ctx)) == []
    assert any("unreadable" in skip for skip in ctx.skips), ctx.skips


def test_an_oserror_reading_the_artifact_is_the_detectors_own_message(
    tmp_path, monkeypatch
):
    """`_find_artifact` proves the path is a file; the open happens after. A
    delete, a permission error, or a Windows share-lock in between raises
    OSError, which the handler did not catch -- the user then read pack.py's
    generic "raised PermissionError" instead of a sentence about coverage."""
    import whetstone.lenses.hygiene.detectors.coverage as coverage_module

    (tmp_path / "coverage.xml").write_text(
        COVERAGE_XML.format(rate="0.72"), encoding="utf-8"
    )

    def _denied(*_args, **_kwargs):
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(coverage_module.ElementTree, "parse", _denied)
    ctx = _ctx(tmp_path, coverage_floor=60)
    assert list(CoverageDetector().detect(ctx)) == []
    assert any(
        skip.startswith("hygiene/coverage:") and "unreadable" in skip
        for skip in ctx.skips
    ), ctx.skips


# --- CodeRabbit round: rounding must not loosen the floor -------------------


def test_the_measurement_is_compared_unrounded(tmp_path):
    """59.9999% is below a floor of 60. Rounding the measurement to 2 decimals
    before the comparison turned it into 60.0 and passed -- the floor above is
    deliberately not truncated for exactly this reason, and rounding the other
    side of the comparison handed the loosening back."""
    (tmp_path / "coverage.xml").write_text(
        COVERAGE_XML.format(rate="0.599999"), encoding="utf-8"
    )
    ctx = _ctx(tmp_path, coverage_floor=60)
    found = list(CoverageDetector().detect(ctx))
    assert len(found) == 1, ctx.skips
    # Display still rounds: the sentence a human reads says 60.0%, and the
    # finding exists because the unrounded value did not clear the floor.
    assert found[0].evidence.data["measured"] == 60.0


def test_a_measurement_exactly_on_the_floor_still_passes(tmp_path):
    """The other half: exact equality is not a failure, so the unrounded
    comparison must not turn a clean project into a finding."""
    (tmp_path / "coverage.xml").write_text(
        COVERAGE_XML.format(rate="0.60"), encoding="utf-8"
    )
    ctx = _ctx(tmp_path, coverage_floor=60)
    assert list(CoverageDetector().detect(ctx)) == []
    assert ctx.skips == []


# --- CodeRabbit round: an `only` entry that names no detector ---------------


def test_an_only_entry_matching_no_detector_is_reported(tmp_path):
    """`only: [covrage]` disabled both detectors, and each skip line said it
    was "not in `only`" -- all true, none of them the reason. The config read
    as applied while it had selected nothing at all."""
    ctx = _ctx(tmp_path, only=["covrage"])
    assert list(HygienePack().run(ctx)) == []
    assert any(
        "no such detector" in skip and "covrage" in skip for skip in ctx.skips
    ), ctx.skips
    # The known ids have to be in the sentence, or the user is told they were
    # wrong without being told what right looks like.
    assert any("coverage" in skip and "deps" in skip for skip in ctx.skips), ctx.skips


def test_a_valid_only_entry_records_no_unmatched_warning(tmp_path):
    """The guard must not fire on a correct config, or it becomes a line
    everybody learns to scroll past."""
    ctx = _ctx(tmp_path, only=["coverage"], coverage_floor=60)
    list(HygienePack().run(ctx))
    assert not any("no such detector" in skip for skip in ctx.skips), ctx.skips
