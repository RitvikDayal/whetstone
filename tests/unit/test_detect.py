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


def test_polyglot_repo_keeps_first_languages_commands_and_says_so(tmp_path):
    # Python is detected first (detect_stack calls _detect_python before
    # _detect_node), so its commands are the ones verified and written; Node's
    # scripts must not silently overwrite them, and evidence["package_manager"]
    # must describe the value actually in effect (uv), not the lockfile of the
    # language that lost.
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        json.dumps(
            {"scripts": {"test": "vitest run", "lint": "eslint .", "build": "next build"}}
        ),
        encoding="utf-8",
    )
    (tmp_path / "pnpm-lock.yaml").write_text("", encoding="utf-8")

    detection = detect_stack(tmp_path)

    assert detection.package_manager == "uv"
    assert detection.commands["test"] == "uv run pytest"
    assert detection.commands["lint"] == "uv run ruff check ."
    assert "build" not in detection.commands  # uv never declares one

    # The evidence for package_manager must agree with the value it explains,
    # not with the language that lost.
    assert detection.evidence["package_manager"] == "found uv.lock"
    assert "pnpm" not in detection.evidence["package_manager"]

    # The user is told what was found but not used, by name.
    note = detection.evidence["javascript_commands_unused"]
    assert "pnpm-lock.yaml" in note
    assert "pnpm test" in note
    assert "pnpm lint" in note
    assert "pnpm build" in note
