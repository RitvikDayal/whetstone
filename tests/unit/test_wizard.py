import io
import json
import sys

import yaml
from rich.console import Console

from whetstone import doctor as doctor_module
from whetstone.config.loader import load_config
from whetstone.doctor import CheckResult
from whetstone.initialize.detect import Detection
from whetstone.initialize.wizard import build_config, render_config, run_wizard


def test_build_config_marks_unverified_commands_out(tmp_path):
    detection = Detection(
        languages=["python"],
        package_manager="uv",
        commands={"test": "uv run pytest", "lint": "uv run ruff check ."},
    )
    cfg = build_config(detection, name="demo", verified={"test": True, "lint": False})
    assert cfg.environment.commands.test == "uv run pytest"
    assert cfg.environment.commands.lint is None


def test_the_header_does_not_claim_dev_was_executed(tmp_path):
    """`dev` is recorded without being launched, so a blanket "every command
    below was executed and exited 0" is false for exactly that line -- and it
    was the line carrying `npm dev`, which could never have exited 0."""
    detection = Detection(
        languages=["javascript"], package_manager="npm", commands={"dev": "npm run dev"}
    )
    cfg = build_config(detection, name="demo", verified={"dev": True})
    text = render_config(cfg)

    assert "dev: npm run dev" in text
    header = text.split("version:")[0]
    assert "except `dev`" in header
    assert "never launched" in header


def test_rendered_config_is_valid_and_commented(tmp_path):
    detection = Detection(languages=["python"], package_manager="uv", commands={})
    cfg = build_config(detection, name="demo", verified={})
    text = render_config(cfg)
    assert text.lstrip().startswith("#")
    parsed = yaml.safe_load(text)
    assert parsed["project"]["name"] == "demo"
    assert parsed["lenses"]["hygiene"]["autonomy"] == 0


def test_run_wizard_writes_a_loadable_config(tmp_path, capsys):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    path = run_wizard(tmp_path, assume_yes=True)
    assert path.name == "whetstone.yaml"
    cfg = load_config(path)
    assert cfg.project.name == tmp_path.name


def test_wizard_refuses_to_overwrite_without_force(tmp_path):
    (tmp_path / "whetstone.yaml").write_text("version: 1\n", encoding="utf-8")
    try:
        run_wizard(tmp_path, assume_yes=True)
    except FileExistsError as exc:
        assert "--force" in str(exc)
    else:
        raise AssertionError("expected FileExistsError")


def test_verified_command_that_fails_is_not_written(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    detection = Detection(
        languages=["python"],
        package_manager="pip",
        commands={"test": f'"{sys.executable}" -c "import sys; sys.exit(1)"'},
    )
    cfg = build_config(detection, name="demo", verified={"test": False})
    assert cfg.environment.commands.test is None


def test_wizard_tells_the_user_about_unused_polyglot_commands(tmp_path, capsys):
    # Real polyglot fixture, not a stub -- both languages actually present on
    # disk, exactly like the detect_stack fixture this mirrors.
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest run"}}), encoding="utf-8"
    )
    (tmp_path / "pnpm-lock.yaml").write_text("", encoding="utf-8")

    run_wizard(tmp_path, assume_yes=True)

    out = capsys.readouterr().out
    assert "pnpm run test" in out
    assert "not used" in out


def test_wizard_says_what_it_is_about_to_execute(tmp_path, monkeypatch):
    """`init --yes` runs the project's own commands in a repo the user may have
    cloned minutes ago and not read. shell=True with cwd=project_root means
    cmd.exe resolves from the working directory before PATH on a default
    Windows box, so `npm install` can be a `npm.bat` sitting in the clone.

    Nothing here can stop that -- running the commands IS what init does. What
    it can do is not be quiet about it: name the commands before running them,
    and name the directory they run in.
    """
    detection = Detection(
        languages=["javascript"],
        package_manager="npm",
        commands={"install": "npm install", "test": "npm run test"},
    )
    monkeypatch.setattr("whetstone.initialize.wizard.detect_stack", lambda root: detection)
    monkeypatch.setattr(
        "whetstone.initialize.wizard.run_command",
        lambda label, command, cwd, timeout: CheckResult(
            name=f"command: {label}", ok=True, detail="stub"
        ),
    )

    # An explicit wide Console, not capsys: rich wraps to the terminal width and
    # a wrapped temp path would make this assert about the wrap, not the notice.
    console = Console(file=io.StringIO(), width=300)
    run_wizard(tmp_path, console=console, assume_yes=True)

    out = console.file.getvalue()
    assert "npm install" in out
    assert "npm run test" in out
    # The directory is half the warning: what runs depends on where it runs.
    assert str(tmp_path) in out
    assert "about to execute" in out.lower()


def test_wizard_says_nothing_scary_when_there_is_nothing_to_run(tmp_path, monkeypatch):
    """A warning that appears when no command will run is noise, and noise is
    how a warning stops being read."""
    monkeypatch.setattr(
        "whetstone.initialize.wizard.detect_stack",
        lambda root: Detection(languages=[], package_manager=None, commands={}),
    )
    console = Console(file=io.StringIO(), width=200)
    run_wizard(tmp_path, console=console, assume_yes=True)
    assert "about to execute" not in console.file.getvalue().lower()


def test_wizard_uses_doctors_per_label_timeouts(tmp_path, monkeypatch):
    detection = Detection(
        languages=["python"],
        package_manager="pip",
        commands={
            "install": "pip install -e .",
            "test": "pytest",
            "lint": "ruff check .",
            "build": "python -m build",
        },
    )
    monkeypatch.setattr("whetstone.initialize.wizard.detect_stack", lambda root: detection)

    seen_timeouts: dict[str, int] = {}

    def _fake_run_command(label, command, cwd, timeout):
        seen_timeouts[label] = timeout
        return CheckResult(name=f"command: {label}", ok=True, detail="stub")

    monkeypatch.setattr("whetstone.initialize.wizard.run_command", _fake_run_command)
    run_wizard(tmp_path, assume_yes=True)

    # Every label's timeout must be the exact value doctor.py uses for the
    # same label -- imported, not copied, so the two cannot drift apart.
    assert seen_timeouts == doctor_module._TIMEOUTS


def test_wizard_confirmed_command_actually_runs(tmp_path, monkeypatch):
    detection = Detection(
        languages=["python"], package_manager="pip", commands={"test": "pytest"}
    )
    monkeypatch.setattr("whetstone.initialize.wizard.detect_stack", lambda root: detection)
    monkeypatch.setattr("sys.stdin", io.StringIO("y\n"))

    def _fake_run_command(label, command, cwd, timeout):
        return CheckResult(name=f"command: {label}", ok=True, detail="stub")

    monkeypatch.setattr("whetstone.initialize.wizard.run_command", _fake_run_command)
    path = run_wizard(tmp_path, assume_yes=False)

    cfg = load_config(path)
    assert cfg.environment.commands.test == "pytest"


def test_wizard_declined_command_never_runs(tmp_path, monkeypatch):
    detection = Detection(
        languages=["python"], package_manager="pip", commands={"test": "pytest"}
    )
    monkeypatch.setattr("whetstone.initialize.wizard.detect_stack", lambda root: detection)
    monkeypatch.setattr("sys.stdin", io.StringIO("n\n"))

    def _fail_if_called(label, command, cwd, timeout):
        raise AssertionError("a declined command must never run")

    monkeypatch.setattr("whetstone.initialize.wizard.run_command", _fail_if_called)
    path = run_wizard(tmp_path, assume_yes=False)

    cfg = load_config(path)
    assert cfg.environment.commands.test is None


def test_wizard_records_dev_without_launching_it(tmp_path, monkeypatch):
    detection = Detection(
        languages=["javascript"], package_manager="pnpm", commands={"dev": "pnpm dev"}
    )
    monkeypatch.setattr("whetstone.initialize.wizard.detect_stack", lambda root: detection)

    def _fail_if_called(label, command, cwd, timeout):
        raise AssertionError("dev must never be launched by the wizard")

    monkeypatch.setattr("whetstone.initialize.wizard.run_command", _fail_if_called)
    path = run_wizard(tmp_path, assume_yes=True)

    cfg = load_config(path)
    assert cfg.environment.commands.dev == "pnpm dev"


def test_wizard_survives_closed_stdin(tmp_path, monkeypatch):
    detection = Detection(
        languages=["python"],
        package_manager="pip",
        commands={"test": "pytest", "lint": "ruff check ."},
    )
    monkeypatch.setattr("whetstone.initialize.wizard.detect_stack", lambda root: detection)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # EOF on the very first read

    def _fail_if_called(label, command, cwd, timeout):
        raise AssertionError("a command must not run when stdin cannot be read")

    monkeypatch.setattr("whetstone.initialize.wizard.run_command", _fail_if_called)
    # Must not raise EOFError.
    path = run_wizard(tmp_path, assume_yes=False)

    cfg = load_config(path)
    assert cfg.environment.commands.test is None
    assert cfg.environment.commands.lint is None
