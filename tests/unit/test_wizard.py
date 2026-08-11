import sys

import yaml

from whetstone.config.loader import load_config
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
