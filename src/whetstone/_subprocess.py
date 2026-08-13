"""Process-tree kill for a subprocess run under a timeout.

Shared by every caller that shells out to a command it does not fully trust
to exit cleanly (`lenses/hygiene/detectors/deps.py`'s pip-audit call,
`doctor.py`'s user-declared commands).

`subprocess.run(timeout=...)` kills only the direct child, then -- on
Windows -- calls `communicate()` a second time with no timeout at all to
gather what is left. A grandchild the command spawned (pip, shelled out to by
pip-audit; node, spawned by npm; a pytest-xdist worker) can still hold the
stdout/stderr pipes open, and that second call then hangs forever, because the
read never reaches EOF while the write end is still held open somewhere. Every
caller here uses `Popen` directly and kills the whole tree instead of relying
on `subprocess.run`'s own timeout handling.

`Popen` is used WITHOUT the `with` form, and that is deliberate rather than an
oversight. `Popen.__exit__` closes the pipes and then, for every exit that is
not a KeyboardInterrupt, calls a bare unbounded `self.wait()` (CPython 3.11
special-cases only KeyboardInterrupt). A caller whose timeout branch RETURNS
from inside the block therefore leaves with `exc_type is None` and takes that
path -- so the whole bounded reap below runs, correctly, and is then followed by
an unbounded wait for the same process it just failed to kill. Measured with the
kill neutered, timeout 2s, `REAP_SECONDS` 15, child sleeping 90s: `kill_and_reap`
returned at t+17.0s exactly as designed, and the caller returned at t+90.1s.
Callers use explicit try/except/finally with `close_pipes` instead, which keeps
the pipe-closing guarantee the context manager was adopted for and drops only
the unbounded wait.
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

# How long to wait for a killed process tree to release its pipes. Bounded, so
# a kill that does not take cannot reintroduce the unbounded wait it exists to
# fix.
REAP_SECONDS = 15


def new_group() -> dict[str, object]:
    """Popen keywords that make the child's whole process tree killable as one
    unit.
    """
    if os.name == "nt":
        # `taskkill /T` walks the parent/child chain directly from the PID, so
        # no group flag is needed here -- and CREATE_NEW_PROCESS_GROUP would
        # also detach the child from console signals for no gain.
        return {}
    return {"start_new_session": True}


def kill_tree(proc: subprocess.Popen) -> None:
    """Kill the child AND anything it started.

    Killing only the direct child leaves a grandchild holding the inherited
    stdout/stderr pipes, so a read that follows never sees EOF.

    Cannot raise. This runs on a timeout/exception path and the caller
    re-raises or returns right after calling it; a cleanup error escaping
    from here would replace what the caller needs to see with itself, and the
    bounded reap after it would never run either.
    """
    try:
        if os.name == "nt":
            # `subprocess.run(timeout=...)` -- the API this module's docstring
            # exists to ban -- with the one property that makes the ban bite
            # removed. That defect is a second unbounded `communicate()` waiting
            # for EOF on pipes an inherited grandchild still holds; DEVNULL means
            # there are no pipes to inherit and none to wait on, so the shape
            # cannot occur here. (taskkill spawns no children either, but that is
            # the weaker argument: it depends on taskkill's behaviour, and this
            # depends only on there being no pipes.) A Popen here would have to
            # be killed on ITS timeout, and killing the killer is a worse
            # answer than removing the hazard.
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=REAP_SECONDS,
            )
        else:
            group = os.getpgid(proc.pid)
            # `new_group()` puts the child in a session of its own, so this is
            # the child's group. A caller that built `Popen` without
            # `**new_group()` -- or a `start_new_session` the platform did not
            # honour -- leaves the child in OUR group, and `killpg` then sends
            # SIGKILL to Whetstone itself, mid-run, with no traceback and no
            # findings written. The helper is package-public and the invariant
            # was documented rather than enforced. Fall through to the
            # direct-child kill in `finally`, which is the correct action for a
            # child that has no group of its own anyway.
            if group != os.getpgid(0):
                os.killpg(group, signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        # Already gone, or the platform refused. Fall through to the direct
        # child so the caller is never left waiting on a live process.
        pass
    finally:
        with contextlib.suppress(OSError):
            proc.kill()


def kill_and_reap(proc: subprocess.Popen) -> bool:
    """Kill the whole tree, then drain what is left of its pipes, bounded.

    The combination every caller wants on the way out of a timeout or an
    interrupt: `kill_tree` above, followed by one more `communicate()` capped
    at `REAP_SECONDS` so a reap that does not take cannot reintroduce the
    unbounded wait this module exists to avoid. Also cannot raise, for the
    same reason `kill_tree` cannot: the caller re-raises or returns its own
    result immediately after calling this.

    Returns True when the drain completed inside the bound, which is also
    exactly when the pipes ended up closed -- `communicate()` closes them on its
    way out. False means the kill did not take and the reader threads are still
    blocked on a pipe somebody still holds; see `close_pipes` for why the pipes
    then have to be left alone.
    """
    kill_tree(proc)
    try:
        proc.communicate(timeout=REAP_SECONDS)
    except subprocess.TimeoutExpired:
        return False
    close_pipes(proc)
    return True


def close_pipes(proc: subprocess.Popen) -> None:
    """Close our ends of the child's pipes. Cannot raise.

    The guarantee `with subprocess.Popen(...)` was adopted for, without the
    unbounded `wait()` that comes attached to it (see the module docstring).
    Idempotent: `communicate()` closes these itself on every path that finishes,
    so in practice this is belt and braces.

    ONLY SAFE ONCE THE READER THREADS HAVE FINISHED, which is why callers reach
    it through `kill_and_reap`'s return value rather than from a bare `finally`.
    On Windows `communicate()` reads stdout and stderr on background threads; if
    one is still blocked on a pipe a surviving grandchild holds, `.close()` here
    blocks on the buffer lock that thread is holding -- for as long as the pipe
    stays open. That is the unbounded wait this module exists to avoid, arrived
    at through the cleanup instead of through `wait()`, and it measured
    identically: 60.1s against a 1s timeout. `Popen.__exit__` closes these same
    streams before its `wait()` and has the same problem.

    So when the reap does not take, the pipes are left open on purpose. The
    process holding them is by definition one that survived a tree kill; its
    handles are released when it finally exits. Leaking two handles until then
    beats hanging the tool forever, and the tool has already reported the
    timeout as a failure by the time this matters.
    """
    for stream in (proc.stdout, proc.stderr, proc.stdin):
        if stream is not None:
            with contextlib.suppress(OSError, ValueError):
                stream.close()


@dataclass(frozen=True)
class ShellResult:
    """What running one shell command actually did.

    `doctor` only ever needed "did it exit 0", and `CheckResult` says exactly
    that. The reproduce stage needs more: WHICH non-zero code, because pytest
    distinguishes "a test failed" (1) from "nothing was collected" (5) from
    "the run was broken" (2-4), and the OUTPUT, because absence has to be
    earned by a marker rather than assumed from an exit code.

    So the execution lives here and both callers take the view they need. One
    execution path, two readings -- rather than a second Popen somewhere else
    that drifts from this one's timeout and kill-tree handling.
    """

    returncode: int
    stdout: str
    stderr: str
    timed_out: bool

    @property
    def output(self) -> str:
        """Both streams, for searching. Order is stdout then stderr because a
        test runner writes its report to stdout and its crashes to stderr."""
        return f"{self.stdout}\n{self.stderr}"


def run_argv(
    argv: Sequence[str], cwd: Path, timeout: int, env: dict[str, str] | None = None
) -> ShellResult:
    """Run *argv* with NO SHELL, bounded. The same handling as `run_shell`.

    This exists because `run_shell` takes a string, and rebuilding an argv into
    one is not a formatting detail -- it is a second parse. The sandbox flattened
    `docker run ... sh -lc "<command>"` by wrapping only the parts containing a
    space in quotes and escaping nothing, and the host shell then re-parsed every
    quote the caller had put inside the command. It broke differently on the two
    CI legs because they have different shells, which is the shape of every
    quoting defect: correct on the machine it was written on.
    """
    proc = subprocess.Popen(
        list(argv),
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        errors="replace",
        env=env,
        **new_group(),
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_and_reap(proc)
        return ShellResult(returncode=-1, stdout="", stderr="", timed_out=True)
    except BaseException:
        kill_and_reap(proc)
        raise
    close_pipes(proc)
    return ShellResult(
        returncode=proc.returncode,
        stdout=stdout or "",
        stderr=stderr or "",
        timed_out=False,
    )


def run_shell(
    command: str, cwd: Path, timeout: int, env: dict[str, str] | None = None
) -> ShellResult:
    """Run *command* through the shell in *cwd*, bounded.

    Extracted from `doctor.run_command`, which now calls it. Every caution in
    that function's docstring applies here and was paid for there: `Popen`
    without the `with` form, `kill_and_reap` on the way out of a timeout AND of
    a KeyboardInterrupt, and `close_pipes` only once the reader threads are
    known to be done.

    *env* replaces the environment entirely when given; None inherits. The
    reproduce stage uses it to stop the interpreter writing bytecode, because
    a `.pyc` left in the target repository is a file Whetstone put there and
    did not remove -- and the worktree sentinel is right to report it.
    """
    proc = subprocess.Popen(
        command,
        cwd=cwd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        # A byte a declared command emits that is not valid UTF-8 -- a test
        # runner echoing a non-ASCII fixture path -- must not crash the run.
        # `replace` is enough: this text is displayed and searched, never
        # stored as an identity, so there is no dedupe key a lost byte could
        # corrupt.
        errors="replace",
        env=env,
        **new_group(),
    )
    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        kill_and_reap(proc)
        return ShellResult(returncode=-1, stdout="", stderr="", timed_out=True)
    except BaseException:
        # KeyboardInterrupt included: a Ctrl-C mid-command must not leave it,
        # or anything it spawned, running with the pipes still open.
        kill_and_reap(proc)
        raise
    close_pipes(proc)
    return ShellResult(
        returncode=proc.returncode,
        stdout=stdout or "",
        stderr=stderr or "",
        timed_out=False,
    )
