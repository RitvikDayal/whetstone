"""The command surface: exit codes, escaping, and error-not-traceback behaviour."""

from __future__ import annotations

import ast
import contextlib
import importlib.metadata
import io
import re
import subprocess
import sys
import textwrap
import tokenize
import traceback
from pathlib import Path

import pytest
from pydantic import BaseModel, SecretStr
from typer.testing import CliRunner

import whetstone
from whetstone.cli import app
from whetstone.errors import WhetstoneError

runner = CliRunner()


def _write_config(root: Path) -> None:
    """A minimal, loadable config with state kept inside *root*.

    `state_dir` is relative on purpose so these tests never touch the real
    `~/.whetstone`, and no lenses are declared so `run` needs no git repo:
    `execute_run` only resolves files for a file-scoped lens.

    NOTE: no `lenses:` key is exactly the shape that used to make `run` report
    a clean project having examined nothing, so this fixture modelled the
    defect. It is kept -- the tests below either never reach a real
    `execute_run` or monkeypatch it -- and
    `test_run_exits_non_zero_when_no_lens_ran` now pins what this shape
    produces, so the fixture cannot go back to being silent unnoticed.
    """
    (root / "whetstone.yaml").write_text(
        "version: 1\nproject:\n  name: demo\nstate_dir: .whetstone-state\n",
        encoding="utf-8",
    )


def test_the_declared_entry_point_resolves():
    """Reads the target out of pyproject's metadata rather than restating it."""
    (entry,) = [
        e
        for e in importlib.metadata.entry_points(group="console_scripts")
        if e.name == "whetstone"
    ]
    assert entry.load() is app


def test_version_command():
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert re.search(r"\d+\.\d+\.\d+", result.stdout)


def test_python_dash_m_runs_the_same_app():
    """`python -m whetstone` died with `No module named whetstone.__main__`.

    Really launched, not imported: __main__ only runs under -m, so importing it
    would prove nothing about the path that was broken. Uses the `version`
    command rather than a `--version` flag: the real CLI exposes version only
    as a command (see the interface list in task-11-brief.md).
    """
    result = subprocess.run(
        [sys.executable, "-m", "whetstone", "version"],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert whetstone.__version__ in result.stdout


def test_bare_invocation_shows_help_and_exits_zero():
    """Running the tool with no arguments is how someone finds out what it
    does, not a usage error.

    Typer/Click's default for a multi-command app with no callback is
    `no_args_is_help` short-circuiting inside `parse_args` before any callback
    runs, printing 'Missing command.' and exiting 2 -- the usage-error code,
    measured directly against this Typer version. `cli.py`'s callback
    overrides that so a bare invocation reads as informative, matching the
    placeholder module's documented rationale (see git history), not as a
    mistake.
    """
    result = runner.invoke(app, [])
    assert "whetstone" in result.output.lower()
    assert "init" in result.output
    assert result.exit_code == 0


def test_run_without_config_exits_nonzero_with_guidance(tmp_path):
    result = runner.invoke(app, ["run", "--path", str(tmp_path)])
    assert result.exit_code != 0
    assert "whetstone init" in result.stdout


@pytest.mark.parametrize("command", ["doctor", "run", "findings", "report"])
def test_every_command_reports_a_missing_config_by_name_not_a_traceback(
    command, tmp_path
):
    """Every command that reads config funnels through the same loader
    (`cli._load`), and a missing config must read as ConfigError's own
    message, never a bare traceback."""
    result = runner.invoke(app, [command, "--path", str(tmp_path)])
    assert result.exit_code != 0
    assert "Traceback" not in result.stdout
    assert "whetstone init" in result.stdout


def test_no_merge_or_deploy_command_is_registered():
    """The help *text* says 'never merges'. No *command* may be named either.

    `names` empty (an unregistered app, or a walk over the wrong attribute
    after a Typer upgrade) satisfies the exclusion below for free, so the
    known command surface is asserted present first -- the scan has to have
    looked at something before its silence means anything.
    """
    names = {command.name or command.callback.__name__ for command in app.registered_commands}
    expected = {"version", "init", "doctor", "run", "findings", "report"}
    assert expected <= names, f"expected commands missing from {names}"
    assert not {name for name in names if "merge" in name or "deploy" in name}


def test_source_contains_no_merge_or_push_invocation():
    """Invariant, asserted mechanically: Whetstone cannot merge or deploy.

    This test file names the forbidden strings itself, so it excludes itself
    from the scan rather than trying to reason about surrounding context.

    The file list is bound and its size asserted before the offender scan: a
    directory move or packaging change that made `src.rglob("*.py")` come up
    empty would otherwise leave `offenders == []` trivially true and this
    invariant silently stops running.
    """
    forbidden = ("git merge", "git push", "gh pr merge", "kubectl apply")
    src = Path(__file__).resolve().parents[2] / "src" / "whetstone"
    files = sorted(src.rglob("*.py"))
    assert len(files) >= 5, f"only found {files}; the scan is not reaching src/"
    offenders = [
        f"{path}: {needle}"
        for path in files
        for needle in forbidden
        if needle in path.read_text(encoding="utf-8")
    ]
    assert offenders == [], offenders


# Two literals in src/ are regex character classes over the surrogate range,
# used to CONTAIN text that cannot be stored -- `lenses/base.py`'s `_SURROGATE`
# and `scope/resolver.py`'s narrower equivalent. They are never printed and could not be
# spelled in ASCII. Allowlisted by exact value rather than by file, so moving
# one keeps it exempt and inventing a third has to be a deliberate edit here.
#
# Built from a TUPLE, not a set literal: ruff's B033 reports these two distinct
# strings as duplicate set items -- `[\ud800-\udfff]` and `[\udc80-\udcff]` are
# not equal, and `assert len(_NOT_CONSOLE_TEXT) == 2` below says so. Passing a
# tuple to frozenset gets the same object without arguing with the lint.
_NOT_CONSOLE_TEXT = frozenset(("[\ud800-\udfff]", "[\udc80-\udcff]"))
assert len(_NOT_CONSOLE_TEXT) == 2, "the two allowlisted regex ranges collapsed"


# `tokenize` reports an f-string as a single STRING token up to Python 3.11 and
# as FSTRING_START / FSTRING_MIDDLE / FSTRING_END from 3.12. A scan filtering on
# STRING alone therefore stopped seeing inside f-strings on the two 3.12 CI legs
# -- silently, because the 3.11 legs went on passing and half a matrix reporting
# clean looks identical to a clean matrix.
#
# The 3.12 names are looked up rather than written, since they do not exist on
# 3.11 and importing them by name would be a collection error there.
_TEXT_TOKENS = frozenset(
    {tokenize.COMMENT, tokenize.STRING}
    | {
        getattr(tokenize, name)
        for name in ("FSTRING_START", "FSTRING_MIDDLE", "FSTRING_END")
        if hasattr(tokenize, name)
    }
)


def _non_ascii_in_text_tokens(source: str, label: str) -> tuple[list[str], int]:
    """Offenders and the number of tokens actually inspected.

    The count is returned rather than kept local so a caller can assert the scan
    looked at something. An empty offender list means nothing on its own.
    """
    offenders: list[str] = []
    inspected = 0
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type not in _TEXT_TOKENS:
            continue
        inspected += 1
        for ch in token.string:
            if ord(ch) > 127:
                offenders.append(f"{label}:{token.start[0]}: {ch!r} (U+{ord(ch):04X})")
    return offenders, inspected


def test_console_output_is_ascii_only():
    """The default Windows console codepage (cp1252) mangles non-ASCII
    punctuation -- an em dash renders as '?' or 'a"', a middle dot as a box.

    This exact defect shipped once: a middle dot crept into `run`'s
    console.print calls when the placeholder cli.py -- whose own comment
    warned about this by name -- was replaced wholesale.

    SCOPE, widened after the narrow version missed a live violation. This used
    to inspect only literal arguments to `console.print`/`console.input`/
    `typer.echo`, and every error in this CLI reaches the console through
    `console.print(f"[red]{exc}[/red]")` instead -- so raise-site text IS
    console output and none of it was scanned. `paths.py`'s "this is usually a
    typo <em dash> point `state_dir` at a directory" was in the tree the whole
    time and rendered as a replacement character under cp1252; measured
    directly, through `whetstone doctor` with PYTHONIOENCODING=cp1252.

    Nor is a raise-site scan enough on its own: that message is not built at
    the `raise`, it is returned by `_state_dir_message` and raised three lines
    later from a variable, which no AST rule follows. So the scan is now every
    string literal in src/ that is not a docstring -- the only boundary that
    does not depend on guessing which literals end up on a console.

    Comments and docstrings are covered by their own tests below rather than
    here, because the reason each is in scope is different: a Typer docstring is
    console output, and a comment is not console output at all but is now held
    to ASCII anyway. `report/html.py` is UTF-8 HTML, not a console, and is
    exempt from all three.

    `inspected` counts every non-docstring string literal this walk actually
    looked at, asserted non-zero before the offender assertion: an empty
    `files` list, or an AST-walk shape change that stopped matching any
    literal, would otherwise leave `offenders == []` trivially true.
    """
    src = Path(__file__).resolve().parents[2] / "src" / "whetstone"
    exempt = {src / "report" / "html.py"}
    files = sorted(src.rglob("*.py"))
    assert len(files) >= 5, f"only found {files}; the scan is not reaching src/"
    offenders: list[str] = []
    inspected = 0
    for path in files:
        if path in exempt:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        docstrings = {
            id(node.body[0].value)
            for node in ast.walk(tree)
            if isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            )
            and node.body
            and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            if id(node) in docstrings or node.value in _NOT_CONSOLE_TEXT:
                continue
            inspected += 1
            for ch in node.value:
                if ord(ch) > 127:
                    offenders.append(f"{path}:{node.lineno}: {ch!r} (U+{ord(ch):04X})")
    assert inspected > 0, "no console-reachable string literals were inspected"
    assert offenders == [], offenders


# The decorators whose docstrings Typer renders. `@app.command()` puts a
# function's docstring in `whetstone --help` (as the command summary) and in
# `whetstone <cmd> --help` (as the description); `@app.callback()` does the same
# for the root. Verified by running both, not inferred from the library.
_TYPER_DECORATORS = frozenset({"command", "callback"})


def _typer_decorated(node: ast.AST) -> bool:
    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    for decorator in node.decorator_list:
        call = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(call, ast.Attribute) and call.attr in _TYPER_DECORATORS:
            return True
    return False


def test_typer_help_text_is_ascii_only():
    """A command's DOCSTRING is console output, and the scan above exempts
    docstrings.

    That exemption is right for the rest of `src/`, where docstrings are prose
    nobody prints -- the codebase uses em dashes throughout and flagging them
    would make the guard noise. It is wrong here: Typer renders the docstring of
    an `@app.command()` function as the command summary in `whetstone --help`
    and as the description in `whetstone <cmd> --help`. So the one category of
    docstring that IS console output had no gate at all, which is the same shape
    of hole the literal scan above was widened to close.
    """
    cli_path = Path(__file__).resolve().parents[2] / "src" / "whetstone" / "cli.py"
    tree = ast.parse(cli_path.read_text(encoding="utf-8"), filename=str(cli_path))
    offenders: list[str] = []
    inspected = 0
    for node in ast.walk(tree):
        if not _typer_decorated(node):
            continue
        doc = ast.get_docstring(node, clean=False)
        if doc is None:
            continue
        inspected += 1
        for ch in doc:
            if ord(ch) > 127:
                offenders.append(f"{cli_path}:{node.lineno}: {ch!r} (U+{ord(ch):04X})")
    # `whetstone --help` lists six commands plus the root callback. Asserting a
    # floor rather than nothing: a decorator rename, or an AST shape change,
    # would otherwise leave `offenders == []` trivially true.
    assert inspected >= 6, f"only {inspected} Typer docstrings were inspected"
    assert offenders == [], offenders


def test_comments_and_docstrings_in_src_are_ascii_only():
    """Comments are not console output. They are held to ASCII regardless.

    The earlier stance was that comments should stay out of scope "because the
    codebase uses em dashes in prose throughout", and that was a fair reading of
    the cost at the time: flagging thirty existing sites would have made the
    guard noise. It is no longer true -- `src/` is ASCII throughout now, apart
    from `report/html.py`, which is a UTF-8 HTML template rather than anything a
    console renders.

    So the trade has inverted. With nothing to flag, the rule costs nothing to
    hold, and holding it mechanically is the difference between a property and a
    periodic cleanup. Without this test the conversion regresses the first time
    someone pastes prose from an editor that smart-quotes, and the next reviewer
    raises it again -- which is exactly how it got raised this time.

    Comments AND docstrings, in one pass over the token stream: `tokenize` sees
    every comment, and the AST scan above deliberately exempts docstrings, so
    this is the only place the two are covered together.
    """
    src = Path(__file__).resolve().parents[2] / "src" / "whetstone"
    exempt = {src / "report" / "html.py"}
    files = sorted(src.rglob("*.py"))
    assert len(files) >= 5, f"only found {files}; the scan is not reaching src/"
    offenders: list[str] = []
    inspected = 0
    for path in files:
        if path in exempt:
            continue
        found, seen = _non_ascii_in_text_tokens(
            path.read_text(encoding="utf-8"), str(path)
        )
        offenders.extend(found)
        inspected += seen
    assert inspected > 0, "no comments or strings were inspected"
    assert offenders == [], offenders


def test_the_ascii_scan_sees_inside_an_f_string():
    """The scan's own regression test, because its blind spot was version-shaped.

    `tokenize` reports an f-string as one STRING token up to 3.11 and as
    FSTRING_START / FSTRING_MIDDLE / FSTRING_END from 3.12, so a scan filtering
    on STRING alone stopped looking inside f-strings on the two 3.12 CI legs
    while the 3.11 legs went on passing. A guard that silently covers less on
    half the matrix is the failure mode this project keeps paying for, so the
    scanner is fed a known offender here rather than trusted.

    Synthetic source, not a real file: `src/` is ASCII, so there is nothing on
    disk that would make this go red if the filter regressed.
    """
    offenders, inspected = _non_ascii_in_text_tokens(
        'x = f"caf\u00e9 {1}"\n', "<synthetic>"
    )
    assert inspected > 0, "no token was inspected at all"
    assert offenders, "a non-ASCII character inside an f-string was not seen"


# --- doctor's exit code is the CI gate ----------------------------------------


def test_doctor_exits_non_zero_when_a_check_fails(tmp_path, monkeypatch):
    _write_config(tmp_path)
    from whetstone.doctor import CheckResult

    monkeypatch.setattr(
        "whetstone.cli.run_doctor",
        lambda cfg, project_root, root: [CheckResult("git", False, "not a repo")],
    )
    result = runner.invoke(app, ["doctor", "--path", str(tmp_path)])
    assert result.exit_code != 0


def test_doctor_exits_zero_when_every_check_passes(tmp_path, monkeypatch):
    _write_config(tmp_path)
    from whetstone.doctor import CheckResult

    monkeypatch.setattr(
        "whetstone.cli.run_doctor",
        lambda cfg, project_root, root: [CheckResult("git", True, "ok")],
    )
    result = runner.invoke(app, ["doctor", "--path", str(tmp_path)])
    assert result.exit_code == 0


# --- run --full: the scope resolver's own error message tells users to pass
# this flag when there is no merge base, so it must actually exist. -----------


def test_run_full_flag_sweeps_instead_of_diffing(tmp_path, monkeypatch):
    _write_config(tmp_path)
    calls = {}

    def _fake_execute_run(conn, cfg, project_root, root, *, tier, changed_only):
        calls["changed_only"] = changed_only
        from whetstone.runner import RunResult

        return RunResult(
            run_id="run-1", tier=tier, file_count=0, status="complete", lens_count=1
        )

    monkeypatch.setattr("whetstone.cli.execute_run", _fake_execute_run)
    result = runner.invoke(app, ["run", "--path", str(tmp_path), "--full"])
    assert result.exit_code == 0, result.stdout
    assert calls["changed_only"] is False


def test_run_without_full_defaults_to_changed_only(tmp_path, monkeypatch):
    _write_config(tmp_path)
    calls = {}

    def _fake_execute_run(conn, cfg, project_root, root, *, tier, changed_only):
        calls["changed_only"] = changed_only
        from whetstone.runner import RunResult

        return RunResult(
            run_id="run-1", tier=tier, file_count=0, status="complete", lens_count=1
        )

    monkeypatch.setattr("whetstone.cli.execute_run", _fake_execute_run)
    result = runner.invoke(app, ["run", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.stdout
    assert calls["changed_only"] is True


# --- report: the invariant a stale report is worse than none is only real
# if the actual CLI wires a run's skips into the actual HTML. ------------------


def test_report_shows_skips_from_the_real_last_run_end_to_end(tmp_path):
    """Through the real CLI and the real store, not a hand-built RunResult.

    `render_report` proves the "Not everything was checked" banner in
    isolation, but `report()` used to call it with `run=None` always, which
    made that banner unreachable through the only command a user has. A
    bogus, unregistered lens produces a real skip via a real `whetstone run`;
    `whetstone report` against that same state must surface it.
    """
    (tmp_path / "whetstone.yaml").write_text(
        "version: 1\n"
        "project:\n"
        "  name: demo\n"
        "state_dir: .whetstone-state\n"
        "lenses:\n"
        "  bogus-lens: {}\n",
        encoding="utf-8",
    )
    run_result = runner.invoke(app, ["run", "--path", str(tmp_path)])
    # Exit 1, not 0: the only declared lens is unregistered, so this run's plan
    # is empty and it examined nothing. That is the C1 rule doing its job on a
    # fixture written before the rule existed -- the skip it produces is still
    # exactly what this test is about.
    assert run_result.exit_code == 1, run_result.stdout
    assert "not installed" in run_result.stdout

    out = tmp_path / "out.html"
    report_result = runner.invoke(
        app, ["report", "--path", str(tmp_path), "--out", str(out)]
    )
    assert report_result.exit_code == 0, report_result.stdout
    html = out.read_text(encoding="utf-8")
    assert "Not everything was checked" in html
    assert "bogus-lens" in html
    assert "not installed" in html


def test_report_writes_a_self_contained_file(tmp_path, assert_self_contained):
    _write_config(tmp_path)
    out = tmp_path / "out.html"
    result = runner.invoke(app, ["report", "--path", str(tmp_path), "--out", str(out)])
    assert result.exit_code == 0, result.stdout
    assert out.exists()
    assert_self_contained(out.read_text(encoding="utf-8"))


def test_report_refuses_a_path_that_escapes_the_project(tmp_path):
    _write_config(tmp_path)
    outside = tmp_path.parent / "escaped-report.html"
    result = runner.invoke(
        app,
        ["report", "--path", str(tmp_path), "--out", "../escaped-report.html"],
    )
    assert result.exit_code != 0
    assert "outside the project root" in result.stdout
    assert not outside.exists()
    assert "Traceback" not in result.stdout


def test_report_refuses_a_directory_target(tmp_path):
    _write_config(tmp_path)
    (tmp_path / "already-a-dir").mkdir()
    result = runner.invoke(
        app, ["report", "--path", str(tmp_path), "--out", "already-a-dir"]
    )
    assert result.exit_code != 0
    assert "directory" in result.stdout
    assert "Traceback" not in result.stdout


def test_report_refuses_a_reserved_device_target(tmp_path):
    """`--out NUL` printed "Wrote ...\\NUL" and exited 0 having written
    nothing at all -- the same shape as every other finding here: a success
    message over work that did not happen.

    The two-word phrase below is checked against WHITESPACE-NORMALIZED
    stdout, not the raw rendering. The error message embeds the full
    `--path`, so under a long enough basetemp Rich's word-wrap lands the
    space between "reserved" and "device" on a line break -- reproduced
    directly: a config directory ~130 chars deep put "a reserved \\ndevice
    on Windows." in the rendered text, and the un-normalized substring check
    failed while exit_code was still 1. Pinning the console width sidesteps
    the concrete repro but not the defect class, since a hostile-enough
    basetemp wraps at any fixed width; collapsing whitespace before the
    substring check reconstructs the sentence regardless of where -- or
    whether -- Rich wrapped it.
    """
    _write_config(tmp_path)
    result = runner.invoke(app, ["report", "--path", str(tmp_path), "--out", "NUL"])
    assert result.exit_code != 0
    normalized_stdout = " ".join(result.stdout.split())
    assert "reserved device" in normalized_stdout
    assert "Traceback" not in result.stdout


# --- a run that examined nothing must not exit 0 ------------------------------


def _set_width(monkeypatch, columns: int) -> None:
    """Pin the width of the console these commands actually print through.

    NOT `monkeypatch.setenv("COLUMNS", ...)`: Rich reads COLUMNS inside
    `Console.__init__` and caches it into `_width`, and `whetstone.cli`'s
    console is constructed at import time. Setting the variable afterwards
    reaches it only when COLUMNS happened to be unset when the module was
    imported, and does nothing at all otherwise -- a test that passes for a
    reason nobody checked. `_width` is what Rich's public `width` setter
    assigns; going through the attribute gets monkeypatch's restoration free.
    """
    from whetstone.cli import console

    monkeypatch.setattr(console, "_width", columns)


def _wide(monkeypatch) -> None:
    """A terminal wide enough that Rich wraps nothing under test."""
    _set_width(monkeypatch, 300)


def test_run_exits_non_zero_when_no_lens_ran(tmp_path, monkeypatch):
    """The whole C1 chain through the real CLI: a config with no `lenses:` key.

    Before: exit 0, "0 new, 0 already known", no skip lines -- against a
    directory that produced 24 findings once a lens was declared. A green
    check that checked nothing is the failure this tool exists to prevent.
    """
    _wide(monkeypatch)
    _write_config(tmp_path)
    result = runner.invoke(app, ["run", "--path", str(tmp_path)])

    assert result.exit_code == 1, result.stdout
    assert "NO LENS RAN" in result.stdout
    assert "no lenses at all" in result.stdout
    assert "Traceback" not in result.stdout


def test_run_exits_zero_when_a_lens_actually_ran(tmp_path, monkeypatch):
    """The counterweight. `run` must not start failing on every ordinary run:
    finding something is still a success, and `doctor` remains the exit-code
    gate for broken infrastructure."""
    _wide(monkeypatch)
    (tmp_path / "whetstone.yaml").write_text(
        "version: 1\n"
        "project:\n"
        "  name: demo\n"
        "state_dir: .whetstone-state\n"
        "lenses:\n"
        "  hygiene:\n"
        "    only: [coverage]\n",
        encoding="utf-8",
    )
    (tmp_path / "coverage.xml").write_text(
        '<?xml version="1.0"?><coverage line-rate="0.10" version="7.0"/>',
        encoding="utf-8",
    )
    result = runner.invoke(app, ["run", "--path", str(tmp_path)])

    assert result.exit_code == 0, result.stdout
    assert "NO LENS RAN" not in result.stdout


def test_run_says_file_not_files_for_a_single_file(tmp_path, monkeypatch):
    _wide(monkeypatch)
    _write_config(tmp_path)

    def _fake_execute_run(conn, cfg, project_root, root, *, tier, changed_only):
        from whetstone.runner import RunResult

        return RunResult(
            run_id="run-1", tier=tier, file_count=1, status="complete", lens_count=1
        )

    monkeypatch.setattr("whetstone.cli.execute_run", _fake_execute_run)
    result = runner.invoke(app, ["run", "--path", str(tmp_path)])
    assert "1 file in scope" in result.stdout
    assert "1 files in scope" not in result.stdout


# --- an unfinished run must not surface as a clean result --------------------


def _config_with_an_interrupted_lens(root: Path) -> None:
    (root / "whetstone.yaml").write_text(
        "version: 1\n"
        "project:\n"
        "  name: demo\n"
        "state_dir: .whetstone-state\n"
        "lenses:\n"
        "  interrupted: {}\n",
        encoding="utf-8",
    )


def _record_a_failed_run(root: Path) -> None:
    """Drive a real interrupted run into *root*'s store, as Ctrl-C would.

    A registered lens that raises KeyboardInterrupt takes the same
    BaseException exit a Ctrl-C during a slow pip-audit takes, and
    `execute_run` closes the row as status='failed' having recorded NO skip --
    which is precisely why the status is the only evidence it happened.
    """
    from whetstone.config.loader import find_config, load_config
    from whetstone.lenses.registry import register
    from whetstone.paths import state_root
    from whetstone.runner import execute_run
    from whetstone.store.db import connect

    class _Interrupted:
        name = "interrupted"
        max_autonomy = 3
        scope = "project"

        def supports_tier(self, tier: str) -> bool:
            return True

        def run(self, ctx):
            raise KeyboardInterrupt
            yield  # pragma: no cover - unreachable; makes this a generator

    register(_Interrupted())
    cfg = load_config(find_config(root))
    state = state_root(root, cfg.state_dir.get_secret_value())
    conn = connect(state)
    with contextlib.suppress(KeyboardInterrupt):
        execute_run(conn, cfg, root, state, tier="quick", changed_only=False)
    row = conn.execute("SELECT status FROM runs").fetchone()
    assert row["status"] == "failed", "fixture did not record a failed run"


@pytest.fixture
def _isolated_registry(monkeypatch):
    """The stub lens above must not leak into other modules in this process."""
    import whetstone.lenses.registry as registry_module

    monkeypatch.setattr(registry_module, "_REGISTRY", dict(registry_module._REGISTRY))


def test_report_says_the_last_run_did_not_finish(
    tmp_path, monkeypatch, _isolated_registry
):
    """C2 end to end through the real CLI and the real store.

    Reproduced before the fix with a real KeyboardInterrupt: the runs table
    said status='failed', and `whetstone report` exited 0 with
    "No open findings." and nothing in the shareable document saying the run
    never finished.
    """
    _wide(monkeypatch)
    _config_with_an_interrupted_lens(tmp_path)
    _record_a_failed_run(tmp_path)

    out = tmp_path / "out.html"
    result = runner.invoke(app, ["report", "--path", str(tmp_path), "--out", str(out)])
    assert result.exit_code == 0, result.stdout
    html = out.read_text(encoding="utf-8")
    assert "did not finish" in html
    assert "failed" in html
    assert "No open findings.</p>" not in html


def test_findings_warns_when_the_last_run_did_not_finish(
    tmp_path, monkeypatch, _isolated_registry
):
    """The console threw away the same truth the report did."""
    _wide(monkeypatch)
    _config_with_an_interrupted_lens(tmp_path)
    _record_a_failed_run(tmp_path)

    result = runner.invoke(app, ["findings", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.stdout
    assert "did not finish" in result.stdout
    assert "failed" in result.stdout


def test_findings_says_so_when_no_run_has_ever_happened(tmp_path, monkeypatch):
    _wide(monkeypatch)
    _write_config(tmp_path)
    result = runner.invoke(app, ["findings", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.stdout
    assert "No run has been recorded" in result.stdout


# --- a killed finding has to look killed -------------------------------------
#
# M1a's Task 10 measurement is the reason this section exists. On the clean
# fixture the falsifier did its job and killed the only candidate, and
# `whetstone findings` then printed that grade D row exactly like run 1's grade
# A: same columns, same colour, no letter anywhere. The falsifier is the whole
# differentiator and its verdict reached no surface a user reads.


def _seed_graded_findings(root: Path) -> None:
    """Three findings in one store: a survivor, a killed one, an ungraded one."""
    from whetstone.config.loader import find_config, load_config
    from whetstone.grade import Grade
    from whetstone.lenses.base import Candidate, Evidence, EvidenceKind, Severity
    from whetstone.paths import state_root
    from whetstone.store.db import connect
    from whetstone.store.findings import upsert

    cfg = load_config(find_config(root))
    conn = connect(state_root(root, cfg.state_dir))
    now = "2026-08-17T10:00:00+00:00"

    def _c(subject, severity, grade, reason):
        return Candidate(
            lens="code-defects" if grade else "hygiene",
            rule_id="R1",
            subject=subject,
            title=f"{subject} is suspicious",
            detail="d",
            severity=severity,
            evidence=Evidence(EvidenceKind.critique, "s", {}),
            grade=grade,
            grade_reason=reason,
        )

    # Subjects deliberately contain no grade letter and no verdict word. The
    # first version of these tests used `killed.py`, and two of them passed
    # against the unfixed CLI because the word came out of the SUBJECT column
    # -- a check that could not fail, which is the defect this project has
    # shipped most often.
    #
    # The falsified one carries the HIGHEST severity on purpose: severity is
    # the model's claim and grade is what survived the gate, so under the
    # severity-first ordering that shipped it sits at the top of the list.
    upsert(conn, _c("z.py", Severity.critical, Grade.D, "the falsifier won"), "r1", now)
    upsert(conn, _c("m.py", Severity.low, Grade.A, "reproduced and survived"), "r1", now)
    upsert(conn, _c("requests", Severity.medium, None, None), "r1", now)
    conn.close()


def _row(stdout: str, subject: str) -> str:
    """The one table line for *subject*. Asserting on the whole of stdout lets
    a word from any other row satisfy the assertion."""
    return next(line for line in stdout.splitlines() if subject in line)


def test_findings_shows_the_grade(tmp_path, monkeypatch):
    _wide(monkeypatch)
    _write_config(tmp_path)
    _seed_graded_findings(tmp_path)

    result = runner.invoke(app, ["findings", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.stdout
    assert "grade" in result.stdout.lower()
    assert "A" in _row(result.stdout, "m.py")
    assert "D" in _row(result.stdout, "z.py")


def test_a_killed_finding_says_killed_rather_than_carrying_a_quiet_letter(
    tmp_path, monkeypatch
):
    """A letter in a column a user skims past is not a distinction.

    The plan's wording is that a grade D finding must be VISUALLY DISTINCT from
    a grade A one. A word is the cheapest signal that survives a screenshot, a
    pipe into a file, and a reader who does not know the grade vocabulary.
    """
    _wide(monkeypatch)
    _write_config(tmp_path)
    _seed_graded_findings(tmp_path)

    result = runner.invoke(app, ["findings", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.stdout
    assert "killed" in _row(result.stdout, "z.py").lower()


def test_findings_are_ordered_by_grade_not_by_the_models_severity_claim(
    tmp_path, monkeypatch
):
    """Severity is what the model claimed. Grade is what survived the gate.

    The falsified row is seeded `critical` and the survivor `low`, so this
    fails against severity-first ordering -- which is what shipped.
    """
    _wide(monkeypatch)
    _write_config(tmp_path)
    _seed_graded_findings(tmp_path)

    result = runner.invoke(app, ["findings", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.stdout
    out = result.stdout
    assert out.index("m.py") < out.index("z.py"), (
        "a grade A finding must not sit below one the falsifier killed"
    )
    assert out.index("requests") < out.index("z.py"), (
        "an ungraded finding from a deterministic lens is not worse than a "
        "killed one; absent is not D"
    )


def test_an_ungraded_finding_is_not_shown_as_killed(tmp_path, monkeypatch):
    """`hygiene` does not grade. Rendering that as D would report a measured
    CVE as something the falsifier dismissed."""
    _wide(monkeypatch)
    _write_config(tmp_path)
    _seed_graded_findings(tmp_path)

    result = runner.invoke(app, ["findings", "--path", str(tmp_path)])
    assert result.exit_code == 0, result.stdout
    row = _row(result.stdout, "requests")
    assert "killed" not in row.lower()
    assert not re.search(r"\b[ABCD]\b", row), row


# --- findings --state is a vocabulary, not free text -------------------------


@pytest.mark.parametrize("bad", ["bogus", "Queued", ""])
def test_findings_rejects_a_state_that_is_not_a_state(tmp_path, monkeypatch, bad):
    """All three used to print "No findings in state 'X'." and exit 0 --
    indistinguishable from a valid state that is genuinely empty."""
    _wide(monkeypatch)
    _write_config(tmp_path)
    result = runner.invoke(app, ["findings", "--path", str(tmp_path), "--state", bad])
    assert result.exit_code != 0
    assert "No findings in state" not in result.output


@pytest.mark.parametrize("good", ["queued", "rejected"])
def test_findings_accepts_every_state_the_store_can_hold(tmp_path, monkeypatch, good):
    _wide(monkeypatch)
    _write_config(tmp_path)
    result = runner.invoke(app, ["findings", "--path", str(tmp_path), "--state", good])
    assert result.exit_code == 0, result.output
    assert f"No findings in state '{good}'." in result.stdout


# --- doctor must not cut the reason off the rows that FAIL --------------------


def test_doctor_shows_the_whole_reason_a_check_failed(tmp_path, monkeypatch):
    """`result.detail[:90]` truncated from the END, where the reason is, and
    kept the START, where a long absolute path is. The git failure is 164
    characters, so the temp path survived and `... is not a git repository.`
    -- the only actionable part -- was destroyed, on exactly the rows that
    exist to say what is broken."""
    _wide(monkeypatch)
    _write_config(tmp_path)
    from whetstone.doctor import CheckResult

    tail = "this-is-the-actionable-tail"
    detail = "C:/some/very/long/absolute/path/that/eats/the/budget" * 3 + f" {tail}"
    assert len(detail) > 90
    monkeypatch.setattr(
        "whetstone.cli.run_doctor",
        lambda cfg, project_root, root: [CheckResult("git", False, detail)],
    )
    result = runner.invoke(app, ["doctor", "--path", str(tmp_path)])
    assert result.exit_code != 0
    assert tail in result.stdout


def test_report_prints_a_path_that_can_be_copied(tmp_path, monkeypatch):
    """Rich wrapped the written path across two lines mid-directory, so the
    one thing on that line worth having could not be pasted. Asserted at a
    NARROW width on purpose -- 80 columns is where it broke."""
    _set_width(monkeypatch, 40)
    _write_config(tmp_path)
    out = tmp_path / "out.html"
    result = runner.invoke(app, ["report", "--path", str(tmp_path), "--out", str(out)])
    assert result.exit_code == 0, result.stdout
    assert str(out) in result.stdout


# --- issue #3, on the PRODUCTION path -----------------------------------------
#
# The first fix scrubbed paths.py's own frames and was tested by calling
# `state_root` directly -- which is structurally unable to see the frame that
# actually leaked. `cli._load` unwrapped the SecretStr into a local one frame
# up, so a `capture_locals` rendering of a real CLI failure still printed the
# credential in full and the issue's headline scenario, a Sentry user shipping
# it to a third party, still happened verbatim.
#
# These tests go through `_load` and through the CLI. A helper-level test is not
# an acceptable substitute for either.

_CLI_SECRET = "state-secret-XYZ-0123456789"


def _config_with_secret_state_dir(tmp_path, monkeypatch) -> Path:
    """A project whose `state_dir` resolves, through `${env:...}`, onto a path
    that already exists as a FILE -- so creating it fails and raises."""
    blocker = tmp_path / _CLI_SECRET
    blocker.write_text("not a directory", encoding="utf-8")
    monkeypatch.setenv("DEMO_STATE", str(blocker / "deep"))
    (tmp_path / "whetstone.yaml").write_text(
        textwrap.dedent(
            """
            version: 1
            project: { name: demo }
            state_dir: "${env:DEMO_STATE}"
            """
        ),
        encoding="utf-8",
    )
    return tmp_path


def _production_locals(exc: BaseException) -> str:
    """Render *exc* the way rich / Sentry do, over the production frames only.

    `tb_next` drops the calling test's frame, whose own `_CLI_SECRET` local
    would otherwise be what the assertion measures.
    """
    tb = exc.__traceback__.tb_next if exc.__traceback__ else None
    return "".join(
        traceback.TracebackException(type(exc), exc, tb, capture_locals=True).format()
    )


def test_load_does_not_leak_the_state_dir_into_capture_locals(tmp_path, monkeypatch):
    from whetstone.cli import _load

    project = _config_with_secret_state_dir(tmp_path, monkeypatch)
    with pytest.raises(WhetstoneError) as caught:
        _load(project)
    rendered = _production_locals(caught.value)
    # Population guard: capture_locals renders one block per frame, and an empty
    # rendering satisfies the absence assertion for free.
    assert "project_root =" in rendered or "cfg =" in rendered, rendered
    assert _CLI_SECRET not in rendered, rendered


def test_the_whole_cli_command_does_not_leak_the_state_dir(tmp_path, monkeypatch):
    """One level further out than `_load`: whatever the command surface does
    with the exception, no frame it passes through may hold the plaintext."""
    project = _config_with_secret_state_dir(tmp_path, monkeypatch)
    result = runner.invoke(app, ["findings", "--path", str(project)])
    assert result.exit_code != 0
    assert _CLI_SECRET not in result.output, result.output
    # Asserted, not branched on. Behind `if result.exception is not None` the
    # locals assertion -- the one that actually reproduces the reported defect,
    # where the output assertion above holds either way -- would stop running
    # the day the command surface handles the error and returns a code instead.
    # It would stay green and say nothing. `CliRunner` catches the exception by
    # default, so this holds on every failing path today.
    assert result.exception is not None, result.output
    rendered = "".join(
        traceback.TracebackException(
            type(result.exception),
            result.exception,
            result.exception.__traceback__,
            capture_locals=True,
        ).format()
    )
    assert _CLI_SECRET not in rendered, rendered


def test_no_production_code_binds_a_resolved_secret_to_a_name():
    """The mechanical guard that keeps the trap from being reset.

    `capture_locals` renders `frame.f_locals`, so a `get_secret_value()` result
    is safe as long as it is passed or returned and never bound. This walks
    `src/` for an assignment whose value contains such a call -- which is
    exactly the shape `cli._load` had.
    """
    src = Path(__file__).resolve().parents[2] / "src" / "whetstone"
    files = sorted(src.rglob("*.py"))
    assert len(files) >= 5, f"only found {files}; the scan is not reaching src/"
    offenders: list[str] = []
    calls_seen = 0
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "get_secret_value"
            ):
                calls_seen += 1
            if not isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign)):
                continue
            if node.value is None:
                continue
            for inner in ast.walk(node.value):
                if (
                    isinstance(inner, ast.Call)
                    and isinstance(inner.func, ast.Attribute)
                    and inner.func.attr == "get_secret_value"
                ):
                    offenders.append(f"{path}:{node.lineno}")
    # Population guard: with no `get_secret_value` call anywhere, an empty
    # offender list says nothing at all.
    assert calls_seen > 0, "no get_secret_value call was found; the scan is vacuous"
    assert offenders == [], (
        "a resolved secret is bound to a local, so capture_locals will render "
        "it. Pass or return the unwrapped value instead:\n  "
        + "\n  ".join(offenders)
    )


def test_no_typed_config_field_is_named_like_a_secret():
    """A latent trap in the #2 fix, guarded rather than left to be discovered.

    `_interpolate` wraps by KEY NAME, so a typed model field named `api_key` or
    `token` would start receiving a `SecretStr` where the annotation says `str`,
    and every config carrying it would fail with a confusing pydantic error.
    No collision exists today; this keeps it that way.
    """
    from whetstone.config import model as model_module
    from whetstone.config.loader import _is_secret_key

    models = [
        value
        for value in vars(model_module).values()
        if isinstance(value, type)
        and issubclass(value, BaseModel)
        and value is not BaseModel
    ]
    assert len(models) >= 5, f"only found {models}; the scan is not reaching the models"
    checked = 0
    offenders: list[str] = []
    for model in models:
        for name, info in model.model_fields.items():
            checked += 1
            if _is_secret_key(name) and info.annotation not in (
                SecretStr,
                SecretStr | None,
            ):
                offenders.append(f"{model.__name__}.{name}: {info.annotation}")
    assert checked > 0, "no model fields were inspected"
    assert offenders == [], (
        "a typed config field is named like a secret, so `_interpolate` will "
        "hand it a SecretStr the annotation does not accept:\n  "
        + "\n  ".join(offenders)
    )
