"""The command surface: exit codes, escaping, and error-not-traceback behaviour."""

from __future__ import annotations

import importlib.metadata
import re
import subprocess
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

import whetstone
from whetstone.cli import app

runner = CliRunner()


def _write_config(root: Path) -> None:
    """A minimal, loadable config with state kept inside *root*.

    `state_dir` is relative on purpose so these tests never touch the real
    `~/.whetstone`, and no lenses are declared so `run` needs no git repo:
    `execute_run` only resolves files for a file-scoped lens.
    """
    (root / "whetstone.yaml").write_text(
        "version: 1\nproject:\n  name: demo\nstate_dir: .whetstone-state\n",
        encoding="utf-8",
    )


def test_the_declared_entry_point_resolves():
    """Reads the target out of pyproject's metadata rather than restating it."""
    (entry,) = [
        e
        for e in importlib.metadata.entry_points(group="console_scripts")
        if e.name == "whetstone"
    ]
    assert entry.load() is app


def test_version_command():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert re.search(r"\d+\.\d+\.\d+", result.stdout)


def test_python_dash_m_runs_the_same_app():
    """`python -m whetstone` died with `No module named whetstone.__main__`.

    Really launched, not imported: __main__ only runs under -m, so importing it
    would prove nothing about the path that was broken. Uses the `version`
    command rather than a `--version` flag: the real CLI exposes version only
    as a command (see the interface list in task-11-brief.md).
    """
    result = subprocess.run(
        [sys.executable, "-m", "whetstone", "version"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert whetstone.__version__ in result.stdout


def test_bare_invocation_shows_help_and_exits_zero():
    """Running the tool with no arguments is how someone finds out what it
    does, not a usage error.

    Typer/Click's default for a multi-command app with no callback is
    `no_args_is_help` short-circuiting inside `parse_args` before any callback
    runs, printing 'Missing command.' and exiting 2 -- the usage-error code,
    measured directly against this Typer version. `cli.py`'s callback
    overrides that so a bare invocation reads as informative, matching the
    placeholder module's documented rationale (see git history), not as a
    mistake.
    """
    result = runner.invoke(app, [])
    assert "whetstone" in result.output.lower()
    assert "init" in result.output
    assert result.exit_code == 0


def test_run_without_config_exits_nonzero_with_guidance(tmp_path):
    result = runner.invoke(app, ["run", "--path", str(tmp_path)])
    assert result.exit_code != 0
    assert "whetstone init" in result.stdout


@pytest.mark.parametrize("command", ["doctor", "run", "findings", "report"])
def test_every_command_reports_a_missing_config_by_name_not_a_traceback(
    command, tmp_path
):
    """Every command that reads config funnels through the same loader
    (`cli._load`), and a missing config must read as ConfigError's own
    message, never a bare traceback."""
    result = runner.invoke(app, [command, "--path", str(tmp_path)])
    assert result.exit_code != 0
    assert "Traceback" not in result.stdout
    assert "whetstone init" in result.stdout


def test_no_merge_or_deploy_command_is_registered():
    """The help *text* says 'never merges'. No *command* may be named either."""
    names = {command.name or command.callback.__name__ for command in app.registered_commands}
    assert not {name for name in names if "merge" in name or "deploy" in name}


def test_source_contains_no_merge_or_push_invocation():
    """Invariant, asserted mechanically: Whetstone cannot merge or deploy.

    This test file names the forbidden strings itself, so it excludes itself
    from the scan rather than trying to reason about surrounding context.
    """
    forbidden = ("git merge", "git push", "gh pr merge", "kubectl apply")
    src = Path(__file__).resolve().parents[2] / "src" / "whetstone"
    offenders = [
        f"{path}: {needle}"
        for path in src.rglob("*.py")
        for needle in forbidden
        if needle in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], offenders


# --- doctor's exit code is the CI gate ----------------------------------------


def test_doctor_exits_non_zero_when_a_check_fails(tmp_path, monkeypatch):
    _write_config(tmp_path)
    from whetstone.doctor import CheckResult

    monkeypatch.setattr(
        "whetstone.cli.run_doctor",
        lambda cfg, project_root, root: [CheckResult("git", False, "not a repo")],
    )
    result = runner.invoke(app, ["doctor", "--path", str(tmp_path)])
    assert result.exit_code != 0


def test_doctor_exits_zero_when_every_check_passes(tmp_path, monkeypatch):
    _write_config(tmp_path)
    from whetstone.doctor import CheckResult

    monkeypatch.setattr(
        "whetstone.cli.run_doctor",
        lambda cfg, project_root, root: [CheckResult("git", True, "ok")],
    )
    result = runner.invoke(app, ["doctor", "--path", str(tmp_path)])
    assert result.exit_code == 0


# --- run --full: the scope resolver's own error message tells users to pass
# this flag when there is no merge base, so it must actually exist. -----------


def test_run_full_flag_sweeps_instead_of_diffing(tmp_path, monkeypatch):
    _write_config(tmp_path)
    calls = {}

    def _fake_execute_run(conn, cfg, project_root, root, *, tier, changed_only):
        calls["changed_only"] = changed_only
        from whetstone.runner import RunResult

        return RunResult(run_id="run-1", tier=tier, file_count=0)

    monkeypatch.setattr("whetstone.cli.execute_run", _fake_execute_run)
    result = runner.invoke(app, ["run", "--path", str(tmp_path), "--full"])
    assert result.exit_code == 0, result.stdout
    assert calls["changed_only"] is False


def test_run_without_full_defaults_to_changed_only(tmp_path, monkeypatch):
    _write_config(tmp_path)
    calls = {}

    def _fake_execute_run(conn, cfg, project_root, root, *, tier, changed_only):
        calls["changed_only"] = changed_only
        from whetstone.runner import RunResult

        return RunResult(run_id="run-1", tier=tier, file_count=0)

    monkeypatch.setattr("whetstone.cli.execute_run", _fake_execute_run)
    result = runner.invoke(app, ["run", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.stdout
    assert calls["changed_only"] is True


# --- report: --out is user-supplied and is not trusted blindly ---------------


def test_report_writes_a_self_contained_file(tmp_path):
    _write_config(tmp_path)
    out = tmp_path / "out.html"
    result = runner.invoke(app, ["report", "--path", str(tmp_path), "--out", str(out)])
    assert result.exit_code == 0, result.stdout
    assert out.exists()
    assert "https://" not in out.read_text(encoding="utf-8").split("</style>")[0]


def test_report_refuses_a_path_that_escapes_the_project(tmp_path):
    _write_config(tmp_path)
    outside = tmp_path.parent / "escaped-report.html"
    result = runner.invoke(
        app,
        ["report", "--path", str(tmp_path), "--out", "../escaped-report.html"],
    )
    assert result.exit_code != 0
    assert "outside the project root" in result.stdout
    assert not outside.exists()
    assert "Traceback" not in result.stdout


def test_report_refuses_a_directory_target(tmp_path):
    _write_config(tmp_path)
    (tmp_path / "already-a-dir").mkdir()
    result = runner.invoke(
        app, ["report", "--path", str(tmp_path), "--out", "already-a-dir"]
    )
    assert result.exit_code != 0
    assert "directory" in result.stdout
    assert "Traceback" not in result.stdout
