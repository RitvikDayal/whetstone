"""The run lock, tested across real processes because that is what it guards.

An in-process test of a cross-process lock proves nothing: `threading.Lock`
passes every assertion below except the ones that spawn an interpreter, and it
is precisely the implementation this module exists not to be.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap

import pytest

from whetstone.runlock import LOCK_FILENAME, RunInProgressError, run_lock

# A child that takes the lock, reports what happened, and exits. `-c` rather
# than a temp file so the test carries its own subject.
_CHILD = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from whetstone.runlock import RunInProgressError, run_lock

    try:
        with run_lock(Path(sys.argv[1])):
            print("ACQUIRED")
    except RunInProgressError:
        print("REFUSED")
    """
)


def _child(state_root) -> str:
    result = subprocess.run(
        [sys.executable, "-c", _CHILD, str(state_root)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_a_second_process_is_refused_while_the_first_holds_it(tmp_path):
    with run_lock(tmp_path):
        assert _child(tmp_path) == "REFUSED"


def test_the_lock_is_released_when_the_holder_exits_normally(tmp_path):
    with run_lock(tmp_path):
        pass
    assert _child(tmp_path) == "ACQUIRED"


def test_the_lock_is_released_when_the_holder_is_KILLED(tmp_path):
    """The whole reason this is an OS lock and not a file that exists.

    A lockfile whose existence is the lock survives this and blocks every
    future run forever. Killed with SIGKILL/TerminateProcess so no cleanup
    handler of any kind gets to run -- not `finally`, not `atexit`, not a
    signal handler.
    """
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            textwrap.dedent(
                """
                import sys, time
                from pathlib import Path
                from whetstone.runlock import run_lock
                with run_lock(Path(sys.argv[1])):
                    print("HOLDING", flush=True)
                    time.sleep(300)
                """
            ),
            str(tmp_path),
        ],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None
        assert holder.stdout.readline().strip() == "HOLDING"
        # Held, demonstrated -- otherwise the release below proves nothing,
        # because a lock that was never taken also "releases".
        assert _child(tmp_path) == "REFUSED"
        holder.kill()
        holder.wait(timeout=30)
    finally:
        if holder.poll() is None:  # pragma: no cover - only on an assert above
            holder.kill()
            holder.wait(timeout=30)

    assert _child(tmp_path) == "ACQUIRED"


def test_the_refusal_names_the_lock_and_says_it_self_releases(tmp_path):
    """The message is the whole recovery path for a user who hits this."""
    with run_lock(tmp_path), pytest.raises(RunInProgressError) as caught, run_lock(tmp_path):
        pass  # pragma: no cover - the raise above is the subject

    message = str(caught.value)
    assert LOCK_FILENAME in message
    assert "released automatically" in message


def test_the_lock_file_holds_nothing(tmp_path):
    """It is a token for the kernel, not a record anyone reads.

    The first version wrote the holding pid into it. On Windows the locked byte
    is MANDATORY-locked and cannot be read back at all -- `read_text` raises
    PermissionError -- so the diagnostic worked on POSIX and not on the
    platform this project targets first. Pinned empty so nobody adds it back.
    """
    with run_lock(tmp_path) as path:
        assert path.exists()
    assert path.stat().st_size == 0


def test_taking_the_lock_creates_the_state_directory(tmp_path):
    fresh = tmp_path / "never-existed"
    with run_lock(fresh):
        assert (fresh / LOCK_FILENAME).exists()


def test_two_different_projects_do_not_contend(tmp_path):
    """The lock is per state directory, not global."""
    one, two = tmp_path / "one", tmp_path / "two"
    with run_lock(one):
        assert _child(two) == "ACQUIRED"
