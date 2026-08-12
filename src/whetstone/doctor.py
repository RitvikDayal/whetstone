"""Preflight. Every declared command is executed, not merely read.

A config that says `test: pytest` proves nothing. `doctor` is the difference
between 'configured correctly' and 'ran correctly', and the predecessor lost
days to that distinction.

On shell=True: these commands come from the user's own config, authored by a
human, and need `&&`, pipes, and PATH resolution to be usable. Model-authored
commands are a different category entirely and get exact-match allowlisting in
M1's policy gate. Do not conflate the two.

Scope of `tests/unit/test_invariants.py`'s merge/push/deploy guard: that test
is a static scan of source TEXT, proving Whetstone's own code contains no
literal invocation of the handful of merge, push, and deploy commands it
names (see that file for the exact list). It cannot see through `shell=True`
into a string that only exists at runtime, so a whetstone.yaml `test:` (or
`build:`, `lint:`, ...) entry built from one of those same words still runs
here, and the guard stays green -- that is the intended boundary, not a gap
the guard failed to catch.

What that boundary actually is, stated carefully because it used to be stated
wrongly here. Authorship is the boundary for the COMMAND STRING: a human's own
config is theirs to write and run, and a model-authored command is a different
category that gets exact-match allowlisting in M1's policy gate instead of
running free-form. Authorship is NOT the boundary for what gets EXECUTED,
because a command string does not name a program -- it names a lookup, and the
lookup is decided by the working directory.

`shell=True` with `cwd=project_root` means cmd.exe resolves the current
directory before PATH. Measured: `git --version` and `npm --version`, run
through this function with a `git.bat` and an `npm.bat` sitting in the project
root, printed `HIJACKED-GIT-VIA-BAT` and `HIJACKED-NPM-VIA-BAT` -- the repo's
files beat `git.EXE` on PATH. Windows ships a mitigation,
`NoDefaultCurrentDirectoryInExePath`, and it was unset at both Machine and User
scope on the machine this was measured on, which is the default. So a human
writing `test: pytest` means their pytest; in a cloned repo that also contains
`pytest.bat` they get the repo's code, and no amount of authorship on the
config side changes that. The real boundary is WHO CONTROLS THE WORKING
DIRECTORY. For `whetstone init` that is whoever wrote the repository, which is
not necessarily the person running it -- see `initialize/wizard.py`, which says
so out loud before it runs anything.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ._subprocess import close_pipes, kill_and_reap, new_group
from .config.model import WhetstoneConfig
from .errors import UnsafeStatePathError
from .paths import assert_not_cloud_synced

# `dev` is deliberately absent: it starts a long-running server and doctor must
# not launch one. M2's browser lens verifies it by booting it with a readiness
# probe and a timeout.
_VERIFIABLE_COMMANDS = ("install", "test", "lint", "build")

_TIMEOUTS = {"install": 600, "test": 900, "lint": 300, "build": 900}


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str
    skipped: bool = False


def run_command(label: str, command: str, cwd: Path, timeout: int) -> CheckResult:
    """Run *command* through the shell and report what its exit code says.

    What a pass here proves, exactly: a process was started from *command* in
    *cwd* and the thing this call spawned exited 0. That is strictly more than
    reading the config, which is the whole reason doctor exists -- and it is
    less than "the command did its job", in two ways worth naming because both
    have been measured rather than imagined.

    A WRAPPER CAN LIE ABOUT ITS CHILD. Only `returncode` is inspected, so any
    shim on PATH that fails to propagate its child's exit code turns a failure
    into a verified pass. pyenv-win's `pip.bat` ends in `call pyenv rehash`,
    which overwrites ERRORLEVEL with rehash's: measured in a directory that is
    not a Python project, the shim exited 0 (`ERROR: ... neither 'setup.py' nor
    'pyproject.toml' found.` on its output) while `pyenv exec pip install -e .`
    underneath it exited 1, and this function returned
    `ok=True, "\\`pip install -e .\\` exited 0."`. `initialize/detect.py` no
    longer generates a bare `pip` for that reason, but a user may write one, and
    nothing here can tell a lying wrapper from an honest success.

    A TOOL CAN EXIT 0 HAVING CHECKED NOTHING. `ruff check .` exits 0 on a
    directory with no Python files in it. Recognising a vacuous pass needs
    per-tool knowledge M0 does not have; see `initialize/detect.py`.

    Uses Popen with a bounded reap (`._subprocess.kill_and_reap`/`new_group`)
    rather than `subprocess.run(timeout=...)`. That helper's own timeout
    handling kills only the direct child and then, on Windows, calls
    `communicate()` a SECOND time with no timeout at all to collect the rest
    of the output -- and that call hangs forever if a grandchild the command
    spawned still holds the stdout/stderr pipes open. `pytest` and `npm test`
    both routinely spawn children, so this is the ordinary case, not an
    exotic one. `lenses/hygiene/detectors/deps.py` hit and fixed the identical
    defect running pip-audit under a timeout; `_subprocess.py` is that fix,
    shared by both call sites.

    Not a `with subprocess.Popen(...)`: its `__exit__` ends in an unbounded
    `wait()` that undid the bound above. `_subprocess.py`'s docstring carries
    the measurement; `close_pipes` in the `finally` keeps the guarantee the
    context manager was there for.
    """
    proc = subprocess.Popen(
        command,
        cwd=cwd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        # A byte a declared command emits that is not valid UTF-8 -- a test
        # runner echoing a non-ASCII fixture path, for instance -- must not
        # crash doctor. `replace` is enough here: unlike deps.py's JSON, this
        # text is only ever displayed, never parsed or stored, so there is no
        # downstream identity or dedupe key that a lost byte could corrupt.
        errors="replace",
        **new_group(),
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # `kill_and_reap` kills the whole tree and then drains what's left
        # of the pipes with its own bounded timeout, so this cannot
        # reintroduce the unbounded wait it exists to avoid. It closes the
        # pipes itself when that drain completes, and deliberately does not
        # when it does not -- closing them then blocks on the reader thread.
        kill_and_reap(proc)
        return CheckResult(
            name=f"command: {label}",
            ok=False,
            detail=f"`{command}` timed out after {timeout}s.",
        )
    except BaseException:
        # KeyboardInterrupt included: a Ctrl-C mid-command must not leave
        # it, or anything it spawned, running with the pipes still open.
        kill_and_reap(proc)
        raise
    # `communicate()` returned, so the reader threads are done and the pipes are
    # already closed. Stated rather than assumed.
    close_pipes(proc)

    if proc.returncode == 0:
        return CheckResult(f"command: {label}", True, f"`{command}` exited 0.")

    tail = (stderr or stdout or "").strip().splitlines()[-5:]
    return CheckResult(
        name=f"command: {label}",
        ok=False,
        detail=f"`{command}` exited {proc.returncode}. " + " ".join(tail),
    )


def run_doctor(
    cfg: WhetstoneConfig, project_root: Path, state_root: Path
) -> list[CheckResult]:
    results: list[CheckResult] = [
        _check_git(project_root),
        _check_state_path(state_root),
    ]

    for label in _VERIFIABLE_COMMANDS:
        command = getattr(cfg.environment.commands, label)
        if not command:
            results.append(
                CheckResult(
                    f"command: {label}", True, "not declared; nothing to verify.", True
                )
            )
            continue
        results.append(run_command(label, command, project_root, _TIMEOUTS[label]))

    dev = cfg.environment.commands.dev
    results.append(
        CheckResult(
            "command: dev",
            True,
            (
                f"declared as `{dev}`; not launched by doctor (long-running)."
                if dev
                else "not declared; nothing to verify."
            ),
            True,
        )
    )
    return results


def _check_git(project_root: Path) -> CheckResult:
    if shutil.which("git") is None:
        return CheckResult("git", False, "git is not on PATH.")
    if not (project_root / ".git").exists():
        return CheckResult("git", False, f"{project_root} is not a git repository.")
    return CheckResult("git", True, "repository detected.")


def _check_state_path(state_root: Path) -> CheckResult:
    try:
        assert_not_cloud_synced(state_root)
    except UnsafeStatePathError as exc:
        return CheckResult("state path", False, str(exc))
    return CheckResult("state path", True, f"{state_root} is local.")
