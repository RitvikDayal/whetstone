import json

from whetstone.initialize.detect import detect_stack


def test_python_uv_project(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    detection = detect_stack(tmp_path)
    assert "python" in detection.languages
    assert detection.package_manager == "uv"
    assert detection.commands["test"] == "uv run pytest"
    assert "uv.lock" in detection.evidence["package_manager"]


def test_node_pnpm_project_uses_declared_scripts(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps(
            {"scripts": {"test": "vitest run", "build": "next build", "dev": "next dev"}}
        ),
        encoding="utf-8",
    )
    (tmp_path / "pnpm-lock.yaml").write_text("", encoding="utf-8")
    detection = detect_stack(tmp_path)
    assert "javascript" in detection.languages
    assert detection.package_manager == "pnpm"
    assert detection.commands["test"] == "pnpm test"
    assert detection.commands["dev"] == "pnpm dev"
    assert "build" in detection.commands


def test_node_without_a_script_does_not_invent_one(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest run"}}), encoding="utf-8"
    )
    (tmp_path / "package-lock.json").write_text("", encoding="utf-8")
    detection = detect_stack(tmp_path)
    assert detection.package_manager == "npm"
    assert "build" not in detection.commands
    assert "dev" not in detection.commands


def test_empty_directory_detects_nothing_and_says_so(tmp_path):
    detection = detect_stack(tmp_path)
    assert detection.languages == []
    assert detection.package_manager is None
    assert detection.commands == {}


def test_polyglot_repo_reports_both(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {}}), encoding="utf-8")
    detection = detect_stack(tmp_path)
    assert set(detection.languages) == {"python", "javascript"}
