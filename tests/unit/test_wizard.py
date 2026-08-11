import io
import json
import sys

import yaml

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
    cfg = build_config(
        tmp_path, detection, name="demo", verified={"test": True, "lint": False}
    )
    assert cfg.environment.commands.test == "uv run pytest"
    assert cfg.environment.commands.lint is None


def test_rendered_config_is_valid_and_commented(tmp_path):
    detection = Detection(languages=["python"], package_manager="uv", commands={})
    cfg = build_config(tmp_path, detection, name="demo", verified={})
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
    cfg = build_config(tmp_path, detection, name="demo", verified={"test": False})
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
    assert "pnpm test" in out
    assert "not used" in out


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
