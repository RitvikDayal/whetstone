"""Detect the project's stack, and record *why* each conclusion was reached.

Detection never invents a command. If package.json declares no `build` script,
no build command is proposed -- an invented command that fails in doctor teaches
the user to distrust the tool on their first minute with it.
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

_PYTHON_COMMANDS = {
    "uv": {"install": "uv sync", "test": "uv run pytest", "lint": "uv run ruff check ."},
    "poetry": {
        "install": "poetry install",
        "test": "poetry run pytest",
        "lint": "poetry run ruff check .",
    },
    "pip": {
        "install": "pip install -e .",
        "test": "pytest",
        "lint": "ruff check .",
    },
}


@dataclass
class Detection:
    languages: list[str] = field(default_factory=list)
    package_manager: str | None = None
    commands: dict[str, str] = field(default_factory=dict)
    evidence: dict[str, str] = field(default_factory=dict)


def detect_stack(project_root: Path) -> Detection:
    detection = Detection()
    _detect_python(project_root, detection)
    _detect_node(project_root, detection)
    return detection


def _detect_python(root: Path, detection: Detection) -> None:
    manifests = [
        name
        for name in ("pyproject.toml", "requirements.txt", "setup.cfg")
        if (root / name).is_file()
    ]
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
    detection.commands.update(_PYTHON_COMMANDS[manager])


def _detect_node(root: Path, detection: Detection) -> None:
    package_json = root / "package.json"
    if not package_json.is_file():
        return

    detection.languages.append("javascript")
    detection.evidence["javascript"] = "found package.json"

    try:
        scripts = json.loads(package_json.read_text(encoding="utf-8")).get("scripts", {})
    except json.JSONDecodeError:
        detection.evidence["javascript"] = "package.json is unparseable; scripts skipped"
        return

    manager = "npm"
    manager_evidence = "no Node lockfile found; assuming npm"
    for lockfile, name in _NODE_LOCKFILES.items():
        if (root / lockfile).is_file():
            manager, manager_evidence = name, f"found {lockfile}"
            break

    node_commands = {"install": f"{manager} install"}
    # Never invent a script the project does not declare.
    for label in ("test", "lint", "build", "dev"):
        if label in scripts:
            node_commands[label] = f"{manager} {label}"

    if detection.package_manager is None:
        # No other language has claimed the command slots yet, so Node's own
        # commands are the ones that get verified and written.
        detection.package_manager = manager
        detection.evidence["package_manager"] = manager_evidence
        detection.commands.update(node_commands)
        return

    # A polyglot repo: Python was detected first and already owns the single
    # command slot per label (`CommandsConfig` has one `test`/`lint`/... field,
    # not one per language). Writing Node's commands on top here would
    # silently replace Python's with no trace, and setting
    # evidence["package_manager"] would describe a value this branch did not
    # produce -- the exact defect this function used to have. Record what was
    # found and left unused instead of guessing which language's commands the
    # user wants.
    unused = ", ".join(f"{label} = `{cmd}`" for label, cmd in sorted(node_commands.items()))
    detection.evidence["javascript_commands_unused"] = (
        f"Node scripts found ({manager_evidence}) but not used: {unused}. "
        f"{detection.package_manager} (Python) already claimed these command "
        "slots -- a project can declare only one command per label today. Add "
        "the Node commands to whetstone.yaml by hand if you need them."
    )
