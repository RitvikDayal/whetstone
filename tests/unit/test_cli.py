"""The console script must at least import and report honestly.

pyproject.toml publishes `whetstone = "whetstone.cli:app"`. Without a cli
module, `pip install .` succeeds and every invocation dies with
ModuleNotFoundError, which the README does not prepare anyone for.
"""

from __future__ import annotations

import importlib.metadata

import pytest
from typer.testing import CliRunner

import whetstone
from whetstone.cli import app

runner = CliRunner()


def test_the_declared_entry_point_resolves():
    """Reads the target out of pyproject's metadata rather than restating it."""
    (entry,) = [
        e
        for e in importlib.metadata.entry_points(group="console_scripts")
        if e.name == "whetstone"
    ]
    assert entry.load() is app


def test_version_flag_exits_clean():
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert whetstone.__version__ in result.output


def test_bare_invocation_shows_help_and_exits_zero():
    """`no_args_is_help` short-circuits in Click and exits 2, which reads as a
    usage error. Running the tool with no arguments is not an error; it is how
    someone finds out what it does."""
    result = runner.invoke(app, [])
    assert "whetstone" in result.output.lower()
    assert "init" in result.output
    assert result.exit_code == 0


@pytest.mark.parametrize("command", ["init", "doctor", "run", "findings", "report"])
def test_unimplemented_commands_say_so_and_exit_non_zero(command):
    result = runner.invoke(app, [command])
    assert result.exit_code != 0
    assert "not implemented yet" in result.output
