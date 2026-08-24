"""One run at a time per state directory, enforced by the operating system.

WHY AN OS LOCK RATHER THAN A LOCKFILE WHOSE EXISTENCE IS THE LOCK. A file that
means "a run is in progress" because it exists survives SIGKILL, a power cut,
and a debugger detach -- so one crashed run permanently blocks every future
one, and the only recovery is deleting a file under a state directory whose
path is a SHA-256 digest. An advisory lock is released by the kernel when the
holding process dies, which is exactly the staleness property the existence of
a file does not have.

WHY CROSS-PROCESS RATHER THAN A `threading.Lock` IN THE SERVER. The race being
guarded is documented in `store/findings.py`: `upsert` checks for a row and
then inserts it in two separate statements, so two callers can interleave. The
second caller is not another thread of the control plane -- it is `whetstone
run` in a terminal, which the control plane cannot see. A lock scoped to the
server process would guard the case that barely happens and miss the case that
motivated it.

THE LOCK FILE HOLDS NOTHING, and the first version held a pid. On Windows
`msvcrt.locking` is MANDATORY rather than advisory, so the locked byte cannot
be read even by the process holding it -- the pid was therefore a diagnostic
readable on POSIX and not on the platform this project targets first, which is
worse than no diagnostic at all. A test asserting the file recorded the pid is
what surfaced it. Nothing here depends on the lock being advisory or mandatory,
only on the kernel releasing it when the holder dies.

NOTHING PREVENTS A PROCESS FROM WRITING TO THE DATABASE WITHOUT TAKING THIS
LOCK. Both entry points take it; a third-party tool poking at the SQLite file
was never in scope.

NOT A REPLACEMENT FOR THE SQLITE BUSY TIMEOUT. `store/db.py` already sets a
30-second timeout for the short window where two connections contend on a
write. This bounds a whole run, which is minutes; the two operate at different
scales and both are needed.
"""

from __future__ import annotations

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .errors import WhetstoneError

LOCK_FILENAME = "run.lock"


class RunInProgressError(WhetstoneError):
    """Another run holds the lock for this project.

    Its own class rather than a bare WhetstoneError: the control plane answers
    this with 409 and every other WhetstoneError with 400, and a caller that
    has to match on message text to tell them apart will eventually match on
    the wrong one.
    """


def _lock_exclusive(handle) -> bool:
    """Take an exclusive non-blocking lock on the first byte. False if held.

    THE FIRST BYTE, on both platforms, because `msvcrt.locking` locks a byte
    range starting at the current file position and has no whole-file mode.
    Locking one agreed byte is the portable intersection, and the file is
    empty because nothing but the kernel ever reads it.
    """
    handle.seek(0)
    if sys.platform == "win32":
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return False
        return True

    import fcntl

    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(handle) -> None:
    """Release. Failures here are deliberately swallowed.

    The lock is released by the kernel when the handle closes and when the
    process exits, so a failure to unlock explicitly changes nothing -- and
    raising out of a `finally` would replace whatever the run itself raised
    with a message about bookkeeping.
    """
    try:
        handle.seek(0)
        if sys.platform == "win32":
            import msvcrt

            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    except OSError:
        pass


@contextmanager
def run_lock(state_root: Path) -> Iterator[Path]:
    """Hold the run lock for *state_root*, or raise RunInProgressError.

    NON-BLOCKING, and refusing rather than queueing is the decision. A caller
    that waits gives a user a terminal that has stopped for no stated reason,
    and a control plane that waits holds an HTTP request open for the length of
    somebody else's run. Refusing says which and lets the caller decide.
    """
    state_root.mkdir(parents=True, exist_ok=True)
    path = state_root / LOCK_FILENAME

    # "a+b", not "w+b". The file is never written to, so truncating it buys
    # nothing -- and on Windows a `w+b` open truncates BEFORE the lock is
    # attempted, which is a write to a file another process is holding.
    handle = open(path, "a+b")  # noqa: SIM115 - closed in the finally below
    try:
        if not _lock_exclusive(handle):
            raise RunInProgressError(
                f"another Whetstone run already holds {path}. Runs are one at a "
                "time per project because two of them write the same findings "
                "database. Wait for it to finish, or stop it -- the lock is "
                "released automatically when that process exits, including if "
                "it is killed."
            )
        try:
            yield path
        finally:
            _unlock(handle)
    finally:
        handle.close()
