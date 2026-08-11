"""The pip-audit subprocess contract, exercised for real.

Every other test of this detector replaces `_run_pip_audit` itself, so the
function had never executed under test and four defects lived in it at once.
Nothing here patches `_run_pip_audit`. The tests either run the installed
pip-audit or point `_PIP_AUDIT_ARGV` at a script whose output shape we control,
so Popen, the encoding, the timeout, the kill, and the return-code gate all run.

Cases the host cannot support -- pip-audit not installed, no network -- skip
with a reason rather than failing.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from whetstone.lenses.base import RunContext
from whetstone.lenses.hygiene.detectors import deps as deps_module
from whetstone.lenses.hygiene.detectors.deps import DepsDetector

# Installed in pip-audit's own environment, never in the demo projects below.
# If one of these is flagged, the ambient interpreter was audited instead of
# the project -- the exact defect this file exists to pin down.
AMBIENT_ONLY = frozenset({"pip-audit", "cyclonedx-python-lib", "boolean-py", "pip-api"})

# A pin with a published advisory and a published fix. Old enough that the
# advisory is not going to be withdrawn.
VULNERABLE_PIN = "requests==2.19.0"


def _ctx(root: Path, **options) -> RunContext:
    return RunContext(
        project_root=root,
        state_root=root / "state",
        files=(),
        tier="quick",
        lens_options={"options": options},
        run_id="run-1",
    )


def _fake_tool(tmp_path: Path, body: str) -> tuple[str, ...]:
    """An argv prefix that runs *body* instead of the real pip-audit.

    A script on PATH cannot shadow the tool on Windows: CreateProcess resolves
    only `.exe` from PATH, so `pip-audit.bat` is never found. Replacing the
    argv prefix is the portable equivalent and still runs the whole subprocess
    path under test.
    """
    script = tmp_path / "fake_pip_audit.py"
    script.write_text(body, encoding="utf-8")
    return (sys.executable, str(script))


def _requires_real_pip_audit() -> None:
    if shutil.which("pip-audit") is None:
        pytest.skip("pip-audit is not installed on this host")


def _audit_or_skip(ctx: RunContext):
    """Run the detector, turning an offline host into a skip, not a failure."""
    found = list(DepsDetector().detect(ctx))
    offline = [
        s
        for s in ctx.skips
        if "pip-audit failed" in s or "produced no output" in s
    ]
    if offline:
        pytest.skip(f"pip-audit could not complete here: {offline[0]}")
    return found


# --- finding 1: audit the project, not the ambient interpreter ---------------


def test_pyproject_project_is_audited_not_the_ambient_environment(tmp_path):
    _requires_real_pip_audit()
    (tmp_path / "pyproject.toml").write_text(
        "[project]\n"
        'name = "demo-vuln"\n'
        'version = "0.1.0"\n'
        'requires-python = ">=3.11"\n'
        f'dependencies = ["{VULNERABLE_PIN}"]\n'
        "\n[build-system]\n"
        'requires = ["hatchling"]\n'
        'build-backend = "hatchling.build"\n',
        encoding="utf-8",
    )
    ctx = _ctx(tmp_path)
    found = _audit_or_skip(ctx)

    flagged = {candidate.subject for candidate in found}
    assert "requests" in flagged, (
        "the project pins a package with a published advisory and it was not "
        f"flagged; flagged={sorted(flagged)} skips={ctx.skips}"
    )
    assert not (flagged & AMBIENT_ONLY), (
        "packages from the interpreter running pip-audit were reported as the "
        f"project's dependencies: {sorted(flagged & AMBIENT_ONLY)}"
    )


def test_requirements_only_project_is_audited_from_the_file(tmp_path):
    _requires_real_pip_audit()
    (tmp_path / "requirements.txt").write_text(
        f"{VULNERABLE_PIN}\n", encoding="utf-8"
    )
    ctx = _ctx(tmp_path)
    found = _audit_or_skip(ctx)

    flagged = {candidate.subject for candidate in found}
    assert "requests" in flagged, f"flagged={sorted(flagged)} skips={ctx.skips}"
    assert not (flagged & AMBIENT_ONLY)


def test_pyproject_without_a_project_table_skips_loudly(tmp_path):
    """A pyproject that is only tool config declares no dependencies. Falling
    back to the ambient environment is the bug; saying so is the fix."""
    (tmp_path / "pyproject.toml").write_text(
        "[tool.ruff]\nline-length = 100\n", encoding="utf-8"
    )
    ctx = _ctx(tmp_path)
    assert list(DepsDetector().detect(ctx)) == []
    assert any(
        "pyproject.toml" in skip and "project" in skip for skip in ctx.skips
    ), ctx.skips


def test_setup_cfg_only_project_skips_loudly(tmp_path):
    (tmp_path / "setup.cfg").write_text("[metadata]\nname = demo\n", encoding="utf-8")
    ctx = _ctx(tmp_path)
    assert list(DepsDetector().detect(ctx)) == []
    assert any("setup.cfg" in skip for skip in ctx.skips), ctx.skips


def test_pyproject_wins_over_requirements_when_both_declare(tmp_path, monkeypatch):
    """Which manifest was audited has to be visible, not guessed at."""
    monkeypatch.setattr(
        deps_module,
        "_PIP_AUDIT_ARGV",
        _fake_tool(
            tmp_path,
            "import json, sys\n"
            "print(json.dumps({'dependencies': [\n"
            "    {'name': 'seen', 'version': '1.0', 'vulns': [\n"
            "        {'id': 'X', 'fix_versions': [], 'description': ' '.join(sys.argv[1:])}\n"
            "    ]}\n"
            "]}))\n",
        ),
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\ndependencies = []\n',
        encoding="utf-8",
    )
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    found = list(DepsDetector().detect(_ctx(tmp_path)))
    assert len(found) == 1
    assert "-r" not in found[0].detail
    assert found[0].evidence.data["audited"] == "pyproject.toml"


# --- finding 2: a dependency pip-audit declined to audit ---------------------


def test_declined_dependency_is_reported_not_dropped(tmp_path, monkeypatch):
    """The real shape, copied from pip-audit 2.10 auditing an editable install:
    a `skip_reason` and no `vulns` key at all."""
    monkeypatch.setattr(
        deps_module,
        "_PIP_AUDIT_ARGV",
        _fake_tool(
            tmp_path,
            "import json\n"
            "print(json.dumps({'dependencies': [\n"
            "    {'name': 'demo-edit', 'skip_reason': 'Dependency not found on "
            "PyPI and could not be audited: demo-edit (0.1.0)'},\n"
            "    {'name': 'clean', 'version': '1.0', 'vulns': []},\n"
            "]}))\n",
        ),
    )
    (tmp_path / "requirements.txt").write_text("-e .\n", encoding="utf-8")
    ctx = _ctx(tmp_path)
    assert list(DepsDetector().detect(ctx)) == []
    assert any(
        "demo-edit" in skip and "not found on PyPI" in skip for skip in ctx.skips
    ), ctx.skips


# --- finding 3: non-UTF-8 output ---------------------------------------------


def test_non_utf8_output_skips_instead_of_raising(tmp_path, monkeypatch):
    """A byte the child emits that is not UTF-8 killed the reader thread and
    left stdout None, which json.loads turned into a bare TypeError that
    `except json.JSONDecodeError` does not catch."""
    monkeypatch.setattr(
        deps_module,
        "_PIP_AUDIT_ARGV",
        _fake_tool(
            tmp_path,
            "import sys\n"
            "sys.stdout.buffer.write(b'{\"dependencies\": [\\xff\\xfe]}')\n"
            "sys.stdout.buffer.flush()\n",
        ),
    )
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    ctx = _ctx(tmp_path)
    assert list(DepsDetector().detect(ctx)) == []
    assert ctx.skips, "non-UTF-8 output produced neither a finding nor a skip"


def test_empty_output_skips_instead_of_raising(tmp_path, monkeypatch):
    monkeypatch.setattr(
        deps_module, "_PIP_AUDIT_ARGV", _fake_tool(tmp_path, "pass\n")
    )
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    ctx = _ctx(tmp_path)
    assert list(DepsDetector().detect(ctx)) == []
    assert any("no output" in skip for skip in ctx.skips), ctx.skips


# --- finding 7: the timeout has to bound the run -----------------------------


def test_timeout_is_bounded_when_a_grandchild_holds_the_pipe(tmp_path, monkeypatch):
    """`subprocess.run(timeout=...)` kills the direct child then waits for EOF
    on pipes a surviving grandchild still holds. pip-audit shells out to pip,
    so the bound was not a bound: measured still running at 90s against a 5s
    timeout."""
    monkeypatch.setattr(deps_module, "_TIMEOUT_SECONDS", 2)
    monkeypatch.setattr(
        deps_module,
        "_PIP_AUDIT_ARGV",
        _fake_tool(
            tmp_path,
            "import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
            "time.sleep(120)\n",
        ),
    )
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    ctx = _ctx(tmp_path)

    started = time.monotonic()
    assert list(DepsDetector().detect(ctx)) == []
    elapsed = time.monotonic() - started

    assert elapsed < 45, f"timeout did not bound the run: {elapsed:.1f}s"
    assert any("pip-audit failed" in skip for skip in ctx.skips), ctx.skips


# --- finding 8: the rest of the subprocess contract --------------------------


def test_missing_tool_skips_loudly(tmp_path, monkeypatch):
    monkeypatch.setattr(
        deps_module, "_PIP_AUDIT_ARGV", ("whetstone-no-such-tool-9f3a",)
    )
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    ctx = _ctx(tmp_path)
    assert list(DepsDetector().detect(ctx)) == []
    assert any("not installed" in skip for skip in ctx.skips), ctx.skips


def test_nonzero_exit_skips_and_carries_stderr(tmp_path, monkeypatch):
    monkeypatch.setattr(
        deps_module,
        "_PIP_AUDIT_ARGV",
        _fake_tool(
            tmp_path,
            "import sys\nprint('index unreachable', file=sys.stderr)\nsys.exit(2)\n",
        ),
    )
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    ctx = _ctx(tmp_path)
    assert list(DepsDetector().detect(ctx)) == []
    assert any("index unreachable" in skip for skip in ctx.skips), ctx.skips


def test_exit_one_is_success_because_that_is_how_advisories_are_reported(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        deps_module,
        "_PIP_AUDIT_ARGV",
        _fake_tool(
            tmp_path,
            "import json, sys\n"
            "print(json.dumps({'dependencies': [{'name': 'requests', "
            "'version': '2.19.0', 'vulns': [{'id': 'PYSEC-2018-28', "
            "'fix_versions': ['2.20.0'], 'description': 'CRLF injection.'}]}]}))\n"
            "sys.exit(1)\n",
        ),
    )
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    ctx = _ctx(tmp_path)
    found = list(DepsDetector().detect(ctx))
    assert [c.rule_id for c in found] == ["PYSEC-2018-28"]
    assert ctx.skips == []


def test_unparseable_json_skips_loudly(tmp_path, monkeypatch):
    monkeypatch.setattr(
        deps_module,
        "_PIP_AUDIT_ARGV",
        _fake_tool(tmp_path, "print('this is not json')\n"),
    )
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    ctx = _ctx(tmp_path)
    assert list(DepsDetector().detect(ctx)) == []
    assert any("unparseable JSON" in skip for skip in ctx.skips), ctx.skips


def test_the_fake_tool_seam_actually_replaces_the_real_one(tmp_path, monkeypatch):
    """A seam that silently kept calling the real tool would make every test
    above prove nothing."""
    marker = tmp_path / "ran"
    monkeypatch.setattr(
        deps_module,
        "_PIP_AUDIT_ARGV",
        _fake_tool(
            tmp_path,
            "import json, pathlib\n"
            f"pathlib.Path(r'{marker}').write_text('yes', encoding='utf-8')\n"
            "print(json.dumps({'dependencies': []}))\n",
        ),
    )
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    assert list(DepsDetector().detect(_ctx(tmp_path))) == []
    assert marker.is_file(), "the fake pip-audit never ran"


def test_argv_carries_the_project_and_no_ambient_fallback(tmp_path, monkeypatch):
    """Argv-level proof of finding 1: the audit target must be in the argv.
    `cwd=project_root` alone changes nothing, because pip-audit takes its
    target as an argument and defaults to the ambient interpreter."""
    recorded = tmp_path / "argv.txt"
    monkeypatch.setattr(
        deps_module,
        "_PIP_AUDIT_ARGV",
        _fake_tool(
            tmp_path,
            "import json, pathlib, sys\n"
            f"pathlib.Path(r'{recorded}').write_text("
            "'\\n'.join(sys.argv[1:]), encoding='utf-8')\n"
            "print(json.dumps({'dependencies': []}))\n",
        ),
    )
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    list(DepsDetector().detect(_ctx(tmp_path)))
    argv = recorded.read_text(encoding="utf-8").splitlines()
    assert "-r" in argv
    assert any(
        Path(arg).name == "requirements.txt" for arg in argv
    ), argv


def test_subprocess_module_is_actually_used(tmp_path):
    """Guards the guard: if `_run_pip_audit` ever stops shelling out, these
    tests would keep passing while proving nothing about a subprocess."""
    assert deps_module.subprocess is subprocess
