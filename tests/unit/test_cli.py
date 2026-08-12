"""The command surface: exit codes, escaping, and error-not-traceback behaviour."""

from __future__ import annotations

import ast
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


def test_console_output_is_ascii_only():
    """The default Windows console codepage (cp1252) mangles non-ASCII
    punctuation -- an em dash renders as '?', a middle dot as a box glyph.

    This exact defect shipped once: a middle dot ('·') crept into `run`'s
    console.print calls when the placeholder cli.py -- whose own comment
    warned about this by name -- was replaced wholesale, and the comment did
    not survive the replacement. A comment alone did not stop it recurring
    once; this scans every literal string argument passed to
    `console.print`/`console.input`/`typer.echo` for a non-ASCII character.

    Comments and docstrings are not console output and are out of scope --
    the codebase uses em dashes in prose throughout. `report/html.py`'s
    template is UTF-8 HTML, not a console, and is explicitly exempt (the
    task brief allows non-ASCII there).
    """
    src = Path(__file__).resolve().parents[2] / "src" / "whetstone"
    exempt = {src / "report" / "html.py"}
    offenders: list[str] = []
    for path in sorted(src.rglob("*.py")):
        if path in exempt:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            target = node.func.value
            is_console_call = (
                node.func.attr in ("print", "input")
                and isinstance(target, ast.Name)
                and target.id == "console"
            ) or (
                node.func.attr == "echo"
                and isinstance(target, ast.Name)
                and target.id == "typer"
            )
            if not is_console_call:
                continue
            for sub in ast.walk(node):
                if not isinstance(sub, ast.Constant) or not isinstance(sub.value, str):
                    continue
                for ch in sub.value:
                    if ord(ch) > 127:
                        offenders.append(
                            f"{path}:{node.lineno}: {ch!r} (U+{ord(ch):04X})"
                        )
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


# --- report: the invariant a stale report is worse than none is only real
# if the actual CLI wires a run's skips into the actual HTML. ------------------


def test_report_shows_skips_from_the_real_last_run_end_to_end(tmp_path):
    """Through the real CLI and the real store, not a hand-built RunResult.

    `render_report` proves the "Not everything was checked" banner in
    isolation, but `report()` used to call it with `run=None` always, which
    made that banner unreachable through the only command a user has. A
    bogus, unregistered lens produces a real skip via a real `whetstone run`;
    `whetstone report` against that same state must surface it.
    """
    (tmp_path / "whetstone.yaml").write_text(
        "version: 1\n"
        "project:\n"
        "  name: demo\n"
        "state_dir: .whetstone-state\n"
        "lenses:\n"
        "  bogus-lens: {}\n",
        encoding="utf-8",
    )
    run_result = runner.invoke(app, ["run", "--path", str(tmp_path)])
    assert run_result.exit_code == 0, run_result.stdout
    assert "not installed" in run_result.stdout

    out = tmp_path / "out.html"
    report_result = runner.invoke(
        app, ["report", "--path", str(tmp_path), "--out", str(out)]
    )
    assert report_result.exit_code == 0, report_result.stdout
    html = out.read_text(encoding="utf-8")
    assert "Not everything was checked" in html
    assert "bogus-lens" in html
    assert "not installed" in html


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
