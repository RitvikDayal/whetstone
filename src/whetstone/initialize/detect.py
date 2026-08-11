"""Detect the project's stack, and record *why* each conclusion was reached.

Two different rules, on purpose, and the difference is worth stating because it
used to be stated wrongly:

**Node reads.** A command is proposed only for a script package.json actually
declares. No `build` script means no build command -- an invented command that
fails in doctor teaches the user to distrust the tool on their first minute
with it.

**Python proposes.** There is no manifest key listing "the test command", so
`_PYTHON_COMMANDS` hands out the conventional spellings for the detected
manager whether or not the project uses them. A pyproject holding nothing but
`[build-system]` still gets `pytest` and `ruff check .` proposed. That is a
guess, and it is only defensible because `init` then RUNS each one and drops
whatever does not exit 0 (`wizard.build_config`) -- the guess never reaches
whetstone.yaml unverified.

The gate is weaker than it looks for `lint`. `ruff check .` exits 0 on a
directory containing no Python files at all ("warning: No Python files found
under the given path(s)" on stderr, "All checks passed!" on stdout, exit 0), so
a verified `lint` proves the linter ran, not that it checked anything. Detecting
a vacuous pass needs per-tool knowledge of what "checked nothing" looks like,
which M0 does not have and will not guess at. Recorded here rather than left for
someone to rediscover.

Every command is a string the user reads and can run themselves, so the
spellings here are the ones that work when typed, not the shortest ones.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

_NODE_LOCKFILES = {
    "pnpm-lock.yaml": "pnpm",
    "package-lock.json": "npm",
    "yarn.lock": "yarn",
    "bun.lockb": "bun",
}

_PYTHON_LOCKFILES = {
    "uv.lock": "uv",
    "poetry.lock": "poetry",
    "Pipfile.lock": "pipenv",
}

# `python -m pip`, never bare `pip`. A `pip` on PATH is frequently a wrapper,
# and a wrapper that does not propagate its child's exit code turns a failed
# install into a verified one -- doctor reads returncode and nothing else.
# pyenv-win's shim is exactly that shape:
#
#     @echo off
#     chcp 1250 > NUL
#     call pyenv exec %~n0 %*
#     call pyenv rehash        <- overwrites ERRORLEVEL with rehash's
#
# Measured in a directory that is not a Python project: the shim exited 0 while
# `pyenv exec pip install -e .` underneath it exited 1, and run_command recorded
# `ok=True, "\`pip install -e .\` exited 0."`. `python -m pip` has no shim in
# the path and exited 1.
_PIP = "python -m pip"

_PYTHON_COMMANDS = {
    "uv": {"install": "uv sync", "test": "uv run pytest", "lint": "uv run ruff check ."},
    "poetry": {
        "install": "poetry install",
        "test": "poetry run pytest",
        "lint": "poetry run ruff check .",
    },
    "pip": {
        "install": f"{_PIP} install -e .",
        "test": "pytest",
        "lint": "ruff check .",
    },
}

# Python manifests that make a directory a Python project, in the order they are
# reported. setup.py belongs here even though `lenses/hygiene/detectors/deps.py`
# leaves it out of its own `_MANIFESTS`: the two lists answer different
# questions. This one asks "is this a Python project", and a setup.py project is
# one. That one asks "can pip-audit be pointed at it", and it cannot.
_PYTHON_MANIFESTS = ("pyproject.toml", "setup.py", "requirements.txt", "setup.cfg")

# Labels read from package.json's `scripts`. `install` is not here: it is a real
# built-in subcommand of every manager, not a script.
_NODE_SCRIPT_LABELS = ("test", "lint", "build", "dev")


# Where a proposed command came from, per label. The distinction is the one the
# module docstring above draws: Node READS a declared script, Python PROPOSES a
# conventional spelling for the detected manager whether or not the project uses
# it. Both used to reach the user labelled "from project manifest", which is
# true of one of them.
MANIFEST = "declared by the project manifest"


def _conventional(manager: str) -> str:
    return f"conventional for {manager}; not declared by the project"


@dataclass
class Detection:
    languages: list[str] = field(default_factory=list)
    package_manager: str | None = None
    commands: dict[str, str] = field(default_factory=dict)
    evidence: dict[str, str] = field(default_factory=dict)
    # label -> provenance, parallel to `commands`. Kept separate rather than
    # folded into a richer command object so the many call sites that read
    # `detection.commands[label]` as a plain string stay unchanged.
    origins: dict[str, str] = field(default_factory=dict)


def detect_stack(project_root: Path) -> Detection:
    detection = Detection()
    _detect_python(project_root, detection)
    _detect_node(project_root, detection)
    return detection


def _detect_python(root: Path, detection: Detection) -> None:
    manifests = [name for name in _PYTHON_MANIFESTS if (root / name).is_file()]
    if not manifests:
        return

    detection.languages.append("python")
    detection.evidence["python"] = f"found {', '.join(manifests)}"

    manager = "pip"
    for lockfile, name in _PYTHON_LOCKFILES.items():
        if (root / lockfile).is_file():
            manager, evidence = name, lockfile
            detection.evidence["package_manager"] = f"found {evidence}"
            break
    else:
        detection.evidence["package_manager"] = (
            f"no Python lockfile found; assuming pip from {manifests[0]}"
        )

    detection.package_manager = manager
    commands = dict(_PYTHON_COMMANDS[manager])

    # `pip install -e .` needs a setup.py or a PEP 621 pyproject to install
    # FROM. A repo carrying only requirements.txt has neither, so the editable
    # install cannot succeed there and `-r requirements.txt` is what was meant.
    if manager == "pip" and not _is_installable(root):
        if (root / "requirements.txt").is_file():
            commands["install"] = f"{_PIP} install -r requirements.txt"
            detection.evidence["python_install"] = (
                "no pyproject.toml or setup.py, so `install -e .` has nothing to "
                "install from; installing from requirements.txt instead."
            )
        else:
            # setup.cfg alone: setuptools can build from it, but only with a
            # shim setup.py or a build-system table, and guessing which is
            # missing would propose a command that cannot work.
            commands.pop("install", None)
            detection.evidence["python_install"] = (
                "only setup.cfg was found, which needs either a setup.py shim or "
                "a [build-system] table before anything can install it, so no "
                "install command is proposed. Add one by hand if you have it."
            )

    detection.commands.update(commands)
    # Every one of these is a guess -- see the module docstring. `install` is a
    # guess too even in the requirements.txt branch: the file proves what to
    # install, never that `pip install -r` is how this project installs it.
    detection.origins.update(dict.fromkeys(commands, _conventional(manager)))


def _is_installable(root: Path) -> bool:
    """True when `pip install -e .` has something to install from."""
    return (root / "pyproject.toml").is_file() or (root / "setup.py").is_file()


def _detect_node(root: Path, detection: Detection) -> None:
    package_json = root / "package.json"
    if not package_json.is_file():
        return

    detection.languages.append("javascript")
    detection.evidence["javascript"] = "found package.json"

    try:
        payload = json.loads(package_json.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        detection.evidence["javascript"] = "package.json is unparseable; scripts skipped"
        return
    except (OSError, UnicodeDecodeError) as exc:
        # A separate branch from the one above, and not a theoretical one:
        # `is_file()` passing says the name resolves to a file, not that this
        # process may read it or that the bytes are UTF-8. A manifest written in
        # a legacy codepage, or one whose ACL excludes the current user, reached
        # `read_text` and left `detect_stack` -- and therefore the first command
        # a new user runs -- as an uncaught traceback.
        detection.evidence["javascript"] = (
            f"package.json could not be read ({type(exc).__name__}: {exc}); "
            "scripts skipped"
        )
        return

    # `json.loads` succeeding proves the file was valid JSON, not that it was an
    # object with a `scripts` mapping. Both of the shapes below reached the
    # membership test below as an uncaught traceback out of the first command a
    # new user runs: `{"scripts": null}` -- which real tooling emits -- raised
    # `TypeError: argument of type 'NoneType' is not iterable`, and a top-level
    # `[]` raised `AttributeError: 'list' object has no attribute 'get'`.
    #
    # Coerced to empty rather than refused, because an unreadable scripts table
    # is not a reason to detect nothing at all -- the manifest still proves this
    # is a Node project, and `install` still works. Recorded in evidence so the
    # user knows the difference between "declares no scripts" and "declares
    # scripts I could not read".
    scripts: object = payload.get("scripts") if isinstance(payload, dict) else None
    if not isinstance(scripts, dict):
        detection.evidence["javascript_scripts"] = (
            f"package.json is valid JSON but its scripts could not be read "
            f"(the file is {type(payload).__name__}, scripts is "
            f"{type(scripts).__name__}); no script commands were proposed. "
            "This is not the same as declaring none."
        )
        scripts = {}

    manager = "npm"
    manager_evidence = "no Node lockfile found; assuming npm"
    for lockfile, name in _NODE_LOCKFILES.items():
        if (root / lockfile).is_file():
            manager, manager_evidence = name, f"found {lockfile}"
            break

    # Every script goes through `run`, for every manager. The bare verb is not a
    # portable shorthand -- it is a coin flip that lands differently per tool:
    #
    #   manager       test  lint  build  dev   (bare verb, exit code)
    #   npm 11.8.0       0     1      1    1   "Unknown command"
    #   pnpm 10.30.0     0     0      0    0
    #   yarn 1.22.22     0     0      0    0
    #   yarn 4.9.2       0     0      0    0
    #   bun 1.3.14       1     0      1    0
    #
    # `<manager> run <script>` exited 0 everywhere, on every manager, for every
    # script.
    #
    # npm aliases only `test` and `start`, so `npm lint` exits 1 having run
    # nothing -- and `dev` is never executed by init at all, so `dev: npm dev`
    # was written into whetstone.yaml under a header promising every command in
    # it had been executed and exited 0. It cannot ever exit 0. npm is also the
    # fallback when no lockfile is found, so this was the majority Node path.
    #
    # bun is why `test` does not stay a direct alias even where one exists.
    # `bun test` is Bun's own test runner and `bun build` its own bundler; both
    # ignore the declared script and run a different program. That failure is
    # silent, and on a project whose layout Bun's runner happens to match it
    # exits 0 having never run what the user declared. A wrong answer that
    # passes verification is worse than one that fails it.
    node_commands = {"install": f"{manager} install"}
    # Never invent a script the project does not declare.
    declared = {
        label: f"{manager} run {label}"
        for label in _NODE_SCRIPT_LABELS
        if label in scripts
    }
    node_commands.update(declared)

    if detection.package_manager is None:
        # No other language has claimed the command slots yet, so Node's own
        # commands are the ones that get verified and written.
        detection.package_manager = manager
        detection.evidence["package_manager"] = manager_evidence
        detection.commands.update(node_commands)
        # `install` is synthesized from the manager, not read from `scripts` --
        # the same "conventional" label Python's proposals carry. Everything in
        # `declared` was read out of the file.
        detection.origins.update(dict.fromkeys(node_commands, _conventional(manager)))
        detection.origins.update(dict.fromkeys(declared, MANIFEST))
        return

    # A polyglot repo: Python was detected first and already owns the single
    # command slot per label (`CommandsConfig` has one `test`/`lint`/... field,
    # not one per language). Writing Node's commands on top here would
    # silently replace Python's with no trace, and setting
    # evidence["package_manager"] would describe a value this branch did not
    # produce -- the exact defect this function used to have. Record what was
    # found and left unused instead of guessing which language's commands the
    # user wants.
    #
    # Built from `declared` and not from `node_commands`: the latter carries the
    # synthesized `install = <manager> install`, and listing that under "Node
    # scripts found" tells the user their package.json declares a script it does
    # not. A package.json declaring no usable script has nothing unused to
    # report, and a note reading "not used: ." is the kind of line that teaches
    # people to skip the notes.
    if not declared:
        return
    unused = ", ".join(f"{label} = `{cmd}`" for label, cmd in sorted(declared.items()))
    detection.evidence["javascript_commands_unused"] = (
        f"Node scripts found ({manager_evidence}) but not used: {unused}. "
        f"{detection.package_manager} (Python) already claimed these command "
        "slots -- a project can declare only one command per label today. Add "
        "the Node commands to whetstone.yaml by hand if you need them."
    )
