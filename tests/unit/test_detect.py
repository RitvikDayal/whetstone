import json
from pathlib import Path

import pytest

from whetstone.initialize.detect import MANIFEST, detect_stack


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
    assert detection.commands["test"] == "pnpm run test"
    assert detection.commands["dev"] == "pnpm run dev"
    assert "build" in detection.commands


# --- the generated invocation has to be one the manager actually understands --
#
# Measured on 2026-08-11, one package.json declaring test/lint/build/dev, each
# script `node -e "console.log(...)"`. Exit codes of the BARE verb:
#
#   manager             test   lint   build   dev
#   npm 11.8.0             0      1       1     1   <- "Unknown command"
#   pnpm 10.30.0           0      0       0     0
#   yarn 1.22.22           0      0       0     0
#   yarn 4.9.2             0      0       0     0
#   bun 1.3.14             1      0       1     0
#
# `<manager> run <script>` exited 0 on every manager for every script.
#
# npm is the plain failure: it aliases only `test` and `start`, so `npm lint`
# exits 1 without running anything, and `npm dev` was WRITTEN INTO the config
# under a header promising every command had been executed and exited 0.
#
# bun is the worse one, and it is why `test` does not stay a direct alias
# anywhere. `bun test` is Bun's own test runner and `bun build` its own
# bundler: both ignore the declared script entirely and run a different
# program. The bare verb there does not fail loudly, it silently substitutes.
# On a project whose tests Bun's runner happens to collect, `bun test` exits 0
# having never run the script the user declared.
#
# So the rule is uniform: scripts go through `run` for every manager. `install`
# stays bare because it is a real built-in subcommand of all four.
_NODE_MANAGERS = (
    ("npm", "package-lock.json"),
    ("pnpm", "pnpm-lock.yaml"),
    ("yarn", "yarn.lock"),
    ("bun", "bun.lockb"),
)


@pytest.mark.parametrize(
    "manager,lockfile", _NODE_MANAGERS, ids=[m for m, _ in _NODE_MANAGERS]
)
def test_every_node_manager_gets_an_invocation_it_understands(
    tmp_path, manager, lockfile
):
    (tmp_path / "package.json").write_text(
        json.dumps(
            {
                "scripts": {
                    "test": "vitest run",
                    "lint": "eslint .",
                    "build": "next build",
                    "dev": "next dev",
                }
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / lockfile).write_text("", encoding="utf-8")

    detection = detect_stack(tmp_path)

    assert detection.package_manager == manager
    assert detection.commands == {
        "install": f"{manager} install",
        "test": f"{manager} run test",
        "lint": f"{manager} run lint",
        "build": f"{manager} run build",
        "dev": f"{manager} run dev",
    }


@pytest.mark.parametrize(
    "manager,lockfile", _NODE_MANAGERS, ids=[m for m, _ in _NODE_MANAGERS]
)
def test_no_script_is_ever_emitted_as_a_bare_verb(tmp_path, manager, lockfile):
    """The property, not the spelling.

    `npm lint` exits 1 and `bun test` runs a different program. Both are the
    same defect -- a script invoked as though it were a subcommand -- so this
    asserts no script command is the two-token bare form, whatever the labels
    grow into later.
    """
    (tmp_path / "package.json").write_text(
        json.dumps(
            {"scripts": {"test": "x", "lint": "x", "build": "x", "dev": "x"}}
        ),
        encoding="utf-8",
    )
    (tmp_path / lockfile).write_text("", encoding="utf-8")

    detection = detect_stack(tmp_path)

    bare = [
        f"{label} = {command}"
        for label, command in detection.commands.items()
        if label != "install" and command == f"{manager} {label}"
    ]
    assert not bare, (
        f"{manager} was handed a bare verb where a script needs `run`: {bare}"
    )


def test_npm_is_the_no_lockfile_fallback_and_still_gets_run(tmp_path):
    """npm is what a repo with no lockfile falls back to, so the majority Node
    path is the one npm's missing aliases break."""
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"lint": "eslint .", "dev": "next dev"}}),
        encoding="utf-8",
    )
    detection = detect_stack(tmp_path)
    assert detection.package_manager == "npm"
    assert detection.commands["lint"] == "npm run lint"
    assert detection.commands["dev"] == "npm run dev"


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
    assert "pnpm run test" in note
    assert "pnpm run lint" in note
    assert "pnpm run build" in note


def test_the_unused_note_lists_only_scripts_the_project_declared(tmp_path):
    """`install` is synthesized by detection, never a declared script, so
    listing it under "Node scripts found ... but not used" tells the user their
    package.json declares something it does not."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest run"}}), encoding="utf-8"
    )
    (tmp_path / "pnpm-lock.yaml").write_text("", encoding="utf-8")

    note = detect_stack(tmp_path).evidence["javascript_commands_unused"]

    assert "pnpm run test" in note
    assert "install" not in note


# --- package.json shapes that are ordinary, and used to be a traceback -------


@pytest.mark.parametrize(
    "payload,why",
    [
        ('{"name":"x","scripts":null}', "real tooling emits a null scripts key"),
        ("[]", "a top-level array"),
        ("null", "a top-level null"),
        ('"hello"', "a top-level string"),
        ('{"scripts":["lint"]}', "scripts as an array"),
        ('{"scripts":"lint"}', "scripts as a string"),
    ],
    ids=["null-scripts", "array", "null", "string", "array-scripts", "string-scripts"],
)
def test_an_odd_package_json_is_reported_not_a_traceback(tmp_path, payload, why):
    """`json.JSONDecodeError` was the only shape handled, and the code then
    assumed a dict with an iterable `scripts`. `{"scripts": null}` raised
    TypeError and `[]` raised AttributeError -- uncaught, out of the first
    command a new user runs."""
    (tmp_path / "package.json").write_text(payload, encoding="utf-8")

    detection = detect_stack(tmp_path)

    assert "javascript" in detection.languages, why
    # No script was declared in any readable way, so none is proposed.
    assert set(detection.commands) <= {"install"}, detection.commands
    # And the user is told the manifest was unreadable rather than empty.
    assert "javascript_scripts" in detection.evidence, detection.evidence


def test_a_readable_package_json_records_no_unreadable_note(tmp_path):
    """The counterweight: the note must not fire on the ordinary shape."""
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest run"}}), encoding="utf-8"
    )
    detection = detect_stack(tmp_path)
    assert "javascript_scripts" not in detection.evidence


def test_unparseable_package_json_still_says_so(tmp_path):
    (tmp_path / "package.json").write_text("{ not json", encoding="utf-8")
    detection = detect_stack(tmp_path)
    assert "javascript" in detection.languages
    assert "unparseable" in detection.evidence["javascript"]


def test_package_json_that_is_not_utf8_records_a_reason(tmp_path):
    """A different branch from the JSON one above: `is_file()` passing says the
    name resolves to a file, not that its bytes decode. A manifest saved in a
    legacy codepage raised UnicodeDecodeError out of `read_text` -- before any
    JSON parsing -- and left `detect_stack` as an uncaught traceback."""
    # 0x80 is a lone continuation byte: valid cp1252, never valid UTF-8.
    (tmp_path / "package.json").write_bytes(b'{"name": "caf\x80"}')

    detection = detect_stack(tmp_path)

    assert "javascript" in detection.languages
    assert "could not be read" in detection.evidence["javascript"]
    assert "UnicodeDecodeError" in detection.evidence["javascript"]


def test_package_json_that_cannot_be_opened_records_a_reason(tmp_path, monkeypatch):
    """The other half of the same branch. A permissions failure or an I/O error
    on read is an OSError, not a decoding error and not a JSON error."""
    (tmp_path / "package.json").write_text("{}", encoding="utf-8")
    real_read_text = Path.read_text

    def _refuse(self, *args, **kwargs):
        if self.name == "package.json":
            raise PermissionError(13, "Permission denied")
        return real_read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", _refuse)

    detection = detect_stack(tmp_path)

    assert "javascript" in detection.languages
    assert "could not be read" in detection.evidence["javascript"]
    assert "PermissionError" in detection.evidence["javascript"]


# --- provenance: which commands the project declared, and which we guessed ---


def test_python_command_proposals_are_labelled_conventional(tmp_path):
    """`_detect_python` hands out `pytest` and `ruff check .` for any Python
    manifest, declared or not -- see detect.py's docstring. The user is about to
    be asked to let those run, so the origin has to say they were guessed."""
    (tmp_path / "pyproject.toml").write_text("[build-system]\n", encoding="utf-8")
    detection = detect_stack(tmp_path)
    assert set(detection.origins) == set(detection.commands), detection.origins
    for label in ("test", "lint", "install"):
        assert "conventional" in detection.origins[label], label
        assert "not declared" in detection.origins[label], label


def test_declared_node_scripts_are_labelled_as_declared(tmp_path):
    """The counterweight: a script read out of `scripts` really did come from
    the manifest, and must not be demoted to a guess."""
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "vitest run", "build": "vite build"}}),
        encoding="utf-8",
    )
    detection = detect_stack(tmp_path)
    assert detection.origins["test"] == MANIFEST
    assert detection.origins["build"] == MANIFEST
    # `npm install` is synthesized from the manager; `scripts` never declared it.
    assert "conventional" in detection.origins["install"]


# --- Python detection: the manifest decides the install command --------------


def test_setup_py_only_project_is_detected(tmp_path):
    """A setup.py project is a Python project. Detecting nothing at all left
    the config empty with no explanation."""
    (tmp_path / "setup.py").write_text(
        "from setuptools import setup\nsetup()\n", encoding="utf-8"
    )
    detection = detect_stack(tmp_path)
    assert "python" in detection.languages
    assert detection.package_manager == "pip"
    assert detection.commands["install"] == "python -m pip install -e ."


def test_requirements_only_project_installs_from_requirements(tmp_path):
    """`pip install -e .` needs a setup.py or a pyproject. A repo carrying only
    requirements.txt has neither, so the editable install cannot work and
    `-r requirements.txt` is what was meant."""
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    detection = detect_stack(tmp_path)
    assert detection.commands["install"] == "python -m pip install -r requirements.txt"


def test_pip_install_goes_through_the_interpreter_not_the_path_shim(tmp_path):
    """A `pip` on PATH may be a wrapper that does not propagate its child's exit
    code -- pyenv-win's `pip.bat` ends in `call pyenv rehash`, which overwrites
    ERRORLEVEL. Measured: `pip install -e .` in a directory that is not a Python
    project exited 0 while the pip underneath it exited 1, and doctor recorded
    the install as verified. `python -m pip` has no shim in the path."""
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n", encoding="utf-8")
    detection = detect_stack(tmp_path)
    assert not detection.commands["install"].startswith("pip ")
    assert detection.commands["install"].startswith("python -m pip ")
