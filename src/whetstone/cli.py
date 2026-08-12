"""The command surface. Everything the tool does has a command."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.console import Console
from rich.table import Table

from . import __version__
from .config.loader import find_config, load_config
from .config.model import Tier
from .doctor import run_doctor
from .errors import ReportError, WhetstoneError
from .initialize.wizard import run_wizard
from .paths import state_root
from .report.html import render_report, write_report
from .runner import execute_run
from .store.db import connect
from .store.findings import list_findings

app = typer.Typer(
    add_completion=False,
    help="Evidence-gated project improvement. Never merges, never deploys.",
)
console = Console()

_PathOption = typer.Option(Path("."), "--path", help="Project root.")
# Module-level singletons, not inline calls: ruff's B008 exempts a bare
# `typer.Option(...)` default when the parameter is a plain builtin type, but
# not for `Tier` (a StrEnum) or a default built from a nested call
# (`Path(...)`) -- both flagged inline. Same fix `_PathOption` already uses.
_TierOption = typer.Option(None, "--tier", help="Override the configured tier.")
_OutOption = typer.Option(Path("whetstone-report.html"), "--out")


# `no_args_is_help` is deliberately NOT set. Click implements it inside
# parse_args, which short-circuits before this callback runs and exits 2 --
# the usage-error code -- making the `invoked_subcommand is None` branch below
# dead code. Running the tool with no arguments is how someone finds out what
# it does, not a usage error, so this callback handles it and exits 0.
@app.callback(invoke_without_command=True)
def main(ctx: typer.Context) -> None:
    if ctx.invoked_subcommand is None:
        console.print(ctx.get_help())
        raise typer.Exit(code=0)


def _load(path: Path):
    config_path = find_config(path)
    cfg = load_config(config_path)
    project_root = config_path.parent
    # `state_dir` is a SecretStr (it may hold a resolved `${env:...}` value;
    # see config/model.py), and `state_root` wants the plain string it wraps.
    override = cfg.state_dir.get_secret_value() if cfg.state_dir is not None else None
    root = state_root(project_root, override)
    return cfg, project_root, root


def _report_target(project_root: Path, out: Path) -> Path:
    """Resolve *out* against *project_root*, refusing to escape it.

    `--out` is user-supplied, and nothing about a report path being wrong
    should be allowed to write outside the project -- same stance
    `state_dir` and the wizard's config target already take on a
    caller-supplied path. `path.resolve()` follows any symlinked ancestor
    directory too, so an escape hidden behind one is caught here; a symlink
    at the final component itself is refused separately by `write_report`.
    """
    target = project_root / out
    try:
        resolved = target.resolve()
        resolved.relative_to(project_root.resolve())
    except ValueError as exc:
        raise ReportError(
            f"--out {out} resolves outside the project root ({project_root}); "
            "refusing to write there."
        ) from exc
    return target


@app.command()
def version() -> None:
    """Print the installed version."""
    console.print(f"whetstone {__version__}")


@app.command(name="init")
def init_command(
    path: Path = _PathOption,
    yes: bool = typer.Option(False, "--yes", "-y", help="Run every check without asking."),
    force: bool = typer.Option(False, "--force", help="Overwrite an existing config."),
) -> None:
    """Create whetstone.yaml, verifying every answer by running it."""
    try:
        run_wizard(path.resolve(), console=console, assume_yes=yes, force=force)
    except (WhetstoneError, FileExistsError) as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc


@app.command()
def doctor(path: Path = _PathOption) -> None:
    """Verify the config against reality. Exits non-zero on any failure."""
    try:
        cfg, project_root, root = _load(path.resolve())
    except WhetstoneError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    results = run_doctor(cfg, project_root, root)
    table = Table("check", "status", "detail")
    for result in results:
        if result.skipped:
            status = "[dim]skipped[/dim]"
        elif result.ok:
            status = "[green]ok[/green]"
        else:
            status = "[red]FAIL[/red]"
        table.add_row(result.name, status, result.detail[:90])
    console.print(table)

    if any(not r.ok for r in results):
        raise typer.Exit(code=1)


@app.command()
def run(
    path: Path = _PathOption,
    tier: Tier | None = _TierOption,
    full: bool = typer.Option(False, "--full", help="Sweep everything, not just the diff."),
) -> None:
    """Find issues.

    Exits 0 whether or not findings were recorded: a run that did its job and
    found something is a success, not a failure -- `doctor` is the exit-code
    gate for infrastructure being broken, and a queued finding is reviewed
    through `whetstone findings` or `whetstone report`, not by failing CI on
    every finding a run turns up.
    """
    try:
        cfg, project_root, root = _load(path.resolve())
        conn = connect(root)
        result = execute_run(
            conn,
            cfg,
            project_root,
            root,
            tier=str(tier or cfg.budget.tier),
            changed_only=not full,
        )
    except WhetstoneError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    console.print(
        f"[bold]{result.new} new[/bold], {result.seen} already known "
        f"· tier {result.tier} · {result.file_count} files in scope"
    )
    if result.skips:
        console.print("\n[yellow]Not everything was checked:[/yellow]")
        for skip in result.skips:
            console.print(f"  · {skip}")


@app.command()
def findings(
    path: Path = _PathOption,
    state: str = typer.Option("queued", "--state", help="Filter by state."),
) -> None:
    """List findings."""
    try:
        cfg, _, root = _load(path.resolve())
        rows = list_findings(connect(root), state=state)
    except WhetstoneError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    if not rows:
        console.print(f"No findings in state '{state}'.")
        return

    table = Table("severity", "lens", "subject", "title")
    for row in rows:
        table.add_row(row.severity, row.lens, row.subject, row.title[:70])
    console.print(table)


@app.command()
def report(
    path: Path = _PathOption,
    out: Path = _OutOption,
) -> None:
    """Write a self-contained HTML report."""
    try:
        cfg, project_root, root = _load(path.resolve())
        rows = list_findings(connect(root), state="queued")
        target = _report_target(project_root, out)
        html = render_report(rows, project_name=cfg.project.name, run=None)
        written = write_report(target, html)
    except WhetstoneError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    console.print(f"[green]Wrote {written}[/green]")
