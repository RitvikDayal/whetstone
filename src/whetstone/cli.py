"""The command surface. Everything the tool does has a command."""

from __future__ import annotations

import contextlib
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
from .runner import RunResult, execute_run, get_last_run
from .store.db import connect
from .store.findings import FindingState, list_findings

app = typer.Typer(
    add_completion=False,
    help="Evidence-gated project improvement. Never merges, never deploys.",
)
console = Console()

# ASCII only in anything that reaches console.print(): the default Windows
# console codepage (cp1252) mangles an em dash to "?" and a middle dot to a
# box glyph. The placeholder cli.py this module replaced carried this same
# warning verbatim, and it was lost when the placeholder was deleted -- a
# middle-dot separator crept back into this exact `run` command as a result
# and shipped once before review caught it. Comments and docstrings are not
# console output and are exempt; `report/html.py`'s template is UTF-8 HTML,
# not a console, and is exempt too.
_PathOption = typer.Option(Path("."), "--path", help="Project root.")
# Module-level singletons, not inline calls: ruff's B008 exempts a bare
# `typer.Option(...)` default when the parameter is a plain builtin type, but
# not for `Tier` (a StrEnum) or a default built from a nested call
# (`Path(...)`) -- both flagged inline. Same fix `_PathOption` already uses.
_TierOption = typer.Option(None, "--tier", help="Override the configured tier.")
_OutOption = typer.Option(
    Path("whetstone-report.html"),
    "--out",
    help="Where to write the report, relative to the project root.",
)
_StateOption = typer.Option(
    FindingState.queued, "--state", help="Filter by finding state."
)


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
    # The SecretStr goes in WRAPPED. Unwrapping it here bound the plaintext to
    # a local of this frame, and `capture_locals` renders every frame a raise
    # passes through -- so a state_dir failure printed the credential in full
    # even after paths.py had scrubbed its own frames. state_root unwraps it
    # internally, where no frame on the traceback holds the result.
    root = state_root(project_root, cfg.state_dir)
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


def _warn_if_the_last_run_did_not_finish(run: RunResult | None) -> None:
    """Say so when the state being listed came from a run that never finished.

    ASCII only -- see the module header.
    """
    if run is None:
        console.print(
            "[yellow]No run has been recorded for this project. Nothing has "
            "been checked; run `whetstone run` first.[/yellow]"
        )
        return
    if not run.finished:
        console.print(
            f"[yellow]Warning: the most recent run did not finish (status "
            f"'{run.status}'). What follows is a partial record of a partial "
            "run - an absent finding may simply never have been looked "
            "for.[/yellow]"
        )


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
        # NOT truncated. `result.detail[:90]` cut from the end, which is where
        # the reason lives and where the path that prefixes it is not: the
        # git-check failure is 164 characters, so 90 kept the temp directory
        # and destroyed `... is not a git repository.` -- the only actionable
        # half, cut off exactly the rows that FAIL. Rich wraps the column to
        # the terminal instead, which loses nothing.
        table.add_row(result.name, status, result.detail)
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

    EXCEPT when no lens ran at all, which exits 1. That is not "did its job and
    found nothing", it is "could not do anything", and the two are identical
    from the outside: `0 new, 0 already known` and exit 0. A config with no
    `lenses:` key produced exactly that against a project carrying 24 real
    findings. A green check that checked nothing is the failure this tool
    exists to prevent, so it is not allowed to be green.

    Exit 1 rather than a distinct code: Click already spends 2 on usage errors
    (`whetstone run --nosuchflag`), and a second meaning for it would be
    ambiguous in the one place -- CI -- where the number is all anyone reads.
    Which failure it is, is in the text.
    """
    try:
        cfg, project_root, root = _load(path.resolve())
        with contextlib.closing(connect(root)) as conn:
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

    files = "file" if result.file_count == 1 else "files"
    console.print(
        f"[bold]{result.new} new[/bold], {result.seen} already known "
        f"- tier {result.tier} - {result.file_count} {files} in scope"
    )
    if result.skips:
        console.print("\n[yellow]Not everything was checked:[/yellow]")
        for skip in result.skips:
            console.print(f"  - {skip}")

    if result.lens_count == 0:
        raise typer.Exit(code=1)


def _grade_cell(grade: str | None) -> str:
    """One cell that a reader can act on without knowing the vocabulary.

    D carries the WORD `killed`, not just the letter. M1a's measurement is the
    reason: on the clean fixture the falsifier killed the only candidate and
    the list rendered it identically to the grade A from the buggy fixture, so
    the differentiator was invisible at the only surface a user reads. A letter
    in a column is a distinction a skimming reader does not make; a word
    survives a screenshot, a pipe into a file, and a colourless terminal.

    An absent grade prints `-`, never `D`. `hygiene` does not grade, and
    rendering "nobody looked" as "the falsifier refuted it" is the exact
    inversion this column exists to prevent.
    """
    if grade is None:
        return "[dim]-[/dim]"
    if grade == "D":
        return "[red]D killed[/red]"
    if grade == "A":
        return "[green]A[/green]"
    if grade == "C":
        return "[yellow]C[/yellow]"
    return grade


@app.command()
def findings(
    path: Path = _PathOption,
    state: FindingState = _StateOption,
) -> None:
    """List findings.

    `--state` is a typed enum, so a typo is a usage error rather than an empty
    list. Untyped, `--state bogus` printed "No findings in state 'bogus'." and
    exited 0, which reads exactly like a valid state with nothing in it.
    """
    try:
        cfg, _, root = _load(path.resolve())
        with contextlib.closing(connect(root)) as conn:
            rows = list_findings(conn, state=str(state))
            last = get_last_run(conn)
    except WhetstoneError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    # The same truth `report` discards: this list is drawn from whatever the
    # last run managed to record, and an interrupted run records less than it
    # was asked to. Printed before the table, because a caveat under a list of
    # zero rows is a caveat nobody reads.
    _warn_if_the_last_run_did_not_finish(last)

    if not rows:
        console.print(f"No findings in state '{state}'.")
        return

    table = Table("grade", "severity", "lens", "subject", "title")
    for row in rows:
        table.add_row(
            _grade_cell(row.grade), row.severity, row.lens, row.subject, row.title[:70]
        )
    console.print(table)

    # Said once, under the table, rather than by hiding the killed rows. A
    # falsified finding is not noise -- it is the falsifier's work made visible,
    # and hiding it by default hides the evidence that the tool discriminates.
    if any(row.grade == "D" for row in rows):
        console.print(
            "[dim]Rows marked killed were refuted by the falsifier. They are "
            "shown, and sorted last, because a tool that quietly drops what it "
            "refuted cannot be checked.[/dim]"
        )


@app.command()
def report(
    path: Path = _PathOption,
    out: Path = _OutOption,
) -> None:
    """Write a self-contained HTML report."""
    try:
        cfg, project_root, root = _load(path.resolve())
        with contextlib.closing(connect(root)) as conn:
            rows = list_findings(conn, state="queued")
            # The most recent run's skips, not None: a report standing in for
            # a run that examined less than it claimed must say so, and it
            # can only say so if the run that produced this state actually
            # reaches the template. See runner.get_last_run for why "most
            # recent" includes a failed run rather than filtering it out.
            run = get_last_run(conn)
        target = _report_target(project_root, out)
        html = render_report(rows, project_name=cfg.project.name, run=run)
        written = write_report(target, html, project_root=project_root)
    except WhetstoneError as exc:
        console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=1) from exc

    # soft_wrap: Rich wraps to the terminal by default, which broke the path
    # across two lines mid-directory and made the one thing on this line worth
    # having -- something to copy and paste -- unusable.
    console.print(f"[green]Wrote[/green] {written}", soft_wrap=True)
