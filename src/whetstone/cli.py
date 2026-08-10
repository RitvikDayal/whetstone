"""Placeholder entry point.

`pyproject.toml` publishes `whetstone = "whetstone.cli:app"`, so without this
module `pip install .` succeeds and every invocation dies with
ModuleNotFoundError. The real CLI arrives with the command implementations; this
exists so the installed console script reports honestly in the meantime.
"""

from __future__ import annotations

import typer

from . import __version__

app = typer.Typer(
    name="whetstone",
    help="Evidence-gated project improvement. It never merges and never deploys.",
    add_completion=False,
    no_args_is_help=True,
)

_PLANNED = {
    "init": "interactive setup; verifies every answer by running it",
    "doctor": "re-verify the config against reality",
    "run": "find issues",
    "findings": "list what it found",
    "report": "write a shareable HTML report",
}


def _unimplemented(name: str) -> None:
    typer.echo(f"whetstone {__version__}")
    # ASCII only: the default Windows console codepage mangles an em dash to "?".
    typer.echo(
        f"`whetstone {name}` is not implemented yet ({_PLANNED[name]}).\n"
        "M0 ships the deterministic core; the commands land with it. See the "
        "roadmap: https://github.com/RitvikDayal/whetstone"
    )
    raise typer.Exit(code=1)


@app.callback(invoke_without_command=True)
def main(
    ctx: typer.Context,
    version: bool = typer.Option(
        False, "--version", help="Show the version and exit.", is_eager=True
    ),
) -> None:
    if version:
        typer.echo(__version__)
        raise typer.Exit(code=0)
    if ctx.invoked_subcommand is None:
        typer.echo(ctx.get_help())
        raise typer.Exit(code=0)


@app.command()
def init() -> None:
    """Interactive setup. Not implemented yet."""
    _unimplemented("init")


@app.command()
def doctor() -> None:
    """Re-verify the config against reality. Not implemented yet."""
    _unimplemented("doctor")


@app.command()
def run() -> None:
    """Find issues. Not implemented yet."""
    _unimplemented("run")


@app.command()
def findings() -> None:
    """List what it found. Not implemented yet."""
    _unimplemented("findings")


@app.command()
def report() -> None:
    """Write a shareable HTML report. Not implemented yet."""
    _unimplemented("report")
