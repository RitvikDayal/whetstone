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
"""

from __future__ import annotations

import contextlib
import os
import signal
import subprocess

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
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=REAP_SECONDS,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        # Already gone, or the platform refused. Fall through to the direct
        # child so the caller is never left waiting on a live process.
        pass
    finally:
        with contextlib.suppress(OSError):
            proc.kill()


def kill_and_reap(proc: subprocess.Popen) -> None:
    """Kill the whole tree, then drain what is left of its pipes, bounded.

    The combination every caller wants on the way out of a timeout or an
    interrupt: `kill_tree` above, followed by one more `communicate()` capped
    at `REAP_SECONDS` so a reap that does not take cannot reintroduce the
    unbounded wait this module exists to avoid. Also cannot raise, for the
    same reason `kill_tree` cannot: the caller re-raises or returns its own
    result immediately after calling this.
    """
    kill_tree(proc)
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.communicate(timeout=REAP_SECONDS)
