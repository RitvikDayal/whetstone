"""Starting a run from the browser, and watching it.

THE FILE IS THE STREAM. Progress events are appended to
`<state>/events/<ticket>.jsonl` and the SSE endpoint tails that file from byte
zero. There is no in-memory fan-out, no pub/sub and no subscriber registry, and
the absence is the design: replay and live are the same code path, so a client
that connects late, reconnects, or opens a second tab is not a special case
anybody has to remember to handle. `runner.execute_run`'s own docstring says
the stream is a convenience and never the record; this is what makes that
literally true rather than aspirational.

WHY A TICKET RATHER THAN THE RUN ID. `execute_run` mints the run id itself, and
only after it has planned the lenses and resolved the file list -- which on a
large repository is seconds. A `POST /api/runs` that waited for it would hold
the request open for that whole period to hand back a string. The server
generates a ticket up front, names the event file after it, and returns
immediately; the real run id arrives inside the stream on the first event. The
ticket is also what keeps this off the filesystem-traversal surface: it is
server-generated hex, validated before it is ever joined to a path.

ONE RUN AT A TIME, ENFORCED BY THE KERNEL. `runlock.run_lock` is an OS advisory
lock, so it also contends with `whetstone run` in a terminal -- which is the
race that matters, and the one an in-process lock would miss entirely.
"""

from __future__ import annotations

import contextlib
import json
import re
import secrets
import threading
import time
from pathlib import Path
from typing import Any

from ..config.model import WhetstoneConfig
from ..errors import WhetstoneError
from ..runlock import run_lock
from ..runner import execute_run
from ..store.db import connect

# Server-generated, so this is a whitelist rather than a sanitiser. Checked
# before the value is joined to a path AND the result is checked to be under
# the events directory -- the regex is the real defence and the containment
# check is the one that survives somebody loosening the regex.
_TICKET = re.compile(r"^[0-9a-f]{32}$")

# How long a stream waits for its first byte before giving up. The run has
# already been accepted at this point, so this only bounds the window between
# `POST /api/runs` returning and the first event being written.
_FIRST_EVENT_TIMEOUT = 60.0

# How often the tail re-reads. Long enough not to spin, short enough that a
# lens finishing feels immediate.
_POLL_SECONDS = 0.2


class UnknownRunError(WhetstoneError):
    """The ticket is not one this server issued, or its stream is gone."""


def new_ticket() -> str:
    return secrets.token_hex(16)


def events_path(state_root: Path, ticket: str) -> Path:
    """The event log for *ticket*, refusing anything that is not one.

    BOTH CHECKS. The pattern is what actually stops a traversal; resolving and
    comparing is what still stops one if the pattern is ever relaxed. Neither
    alone is the belt-and-braces -- they fail in different directions.
    """
    if not _TICKET.match(ticket):
        raise UnknownRunError(
            f"{ticket!r} is not a run ticket. Tickets are issued by "
            "`POST /api/runs` and are 32 hex characters."
        )
    directory = (state_root / "events").resolve()
    path = (directory / f"{ticket}.jsonl").resolve()
    if path.parent != directory:
        raise UnknownRunError(f"{ticket!r} does not resolve inside {directory}.")
    return path


class _EventLog:
    """Append-only JSONL, one event per line, flushed on every write.

    FLUSHED EVERY TIME, and the cost is deliberate. A buffered writer means a
    run that takes four minutes shows nothing for four minutes and then
    everything at once, which is indistinguishable from a hung run on the one
    surface that exists to say it is not hung.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def write(self, event: dict[str, Any]) -> None:
        line = json.dumps(event, default=str)
        # A threading lock, and here it IS the right scope: the contention is
        # between the run thread and the failure path in `_run`, both inside
        # this process. Cross-process contention is handled one layer up by the
        # run lock, which means no second process is writing this file at all.
        with self._lock, self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
            handle.flush()


def start(
    *,
    config: WhetstoneConfig,
    project_root: Path,
    state_root: Path,
    tier: str,
    changed_only: bool,
) -> str:
    """Take the run lock, start the run on its own thread, return the ticket.

    THE LOCK IS TAKEN HERE, NOT IN THE THREAD, so `POST /api/runs` can answer
    409 synchronously when a run is already in progress. Acquiring it in the
    worker would mean accepting the request, returning a ticket, and only then
    discovering the run cannot happen -- a caller that has to open a stream to
    find out its request was refused.

    The held lock is handed to the thread through an `ExitStack`, which the
    thread closes in its own `finally`. That is why this is not a `with`
    statement: the lock outlives this function by design.
    """
    ticket = new_ticket()
    log = _EventLog(events_path(state_root, ticket))

    stack = contextlib.ExitStack()
    # Raises RunInProgressError, which the route turns into 409. Nothing has
    # been created or spawned at this point.
    stack.enter_context(run_lock(state_root))

    def _run() -> None:
        try:
            # ITS OWN CONNECTION. `sqlite3` connections are not safe to share
            # across threads, and the request handler's connection belongs to a
            # request that has already returned.
            with contextlib.closing(connect(state_root)) as conn:
                execute_run(
                    conn,
                    config,
                    project_root,
                    state_root,
                    tier=tier,
                    changed_only=changed_only,
                    on_event=log.write,
                )
        except BaseException as exc:  # noqa: BLE001 - the watcher must be told
            # A run that dies has to close its own stream, or every client
            # watching it waits forever on a `run_finished` that is not coming.
            # `BaseException` on purpose: a KeyboardInterrupt in the worker is
            # exactly the case where a silent stream is worst. Re-raising is
            # pointless -- this is the top of a thread and nobody is above it to
            # catch it -- so it is recorded instead.
            log.write(
                {
                    "kind": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
        finally:
            stack.close()

    threading.Thread(target=_run, name=f"whetstone-run-{ticket}", daemon=True).start()
    return ticket


def tail(path: Path, *, stop: threading.Event | None = None):
    """Yield every event in *path*, then follow it until the run ends.

    A GENERATOR OVER BYTES, not lines held in memory: the offset is carried
    across polls so a partially-written final line is re-read rather than
    yielded half-formed. JSONL plus a flush per event makes a torn line
    unlikely; "unlikely" is not a guarantee, so a line that does not parse is
    treated as not yet complete and retried.

    Terminates on `run_finished` or `error`, which are the only two events that
    can be last. A stream that never terminates holds a request open for the
    life of the server.
    """
    deadline = time.monotonic() + _FIRST_EVENT_TIMEOUT
    offset = 0
    pending = ""

    while True:
        if stop is not None and stop.is_set():
            return
        try:
            with path.open("r", encoding="utf-8") as handle:
                handle.seek(offset)
                chunk = handle.read()
                offset = handle.tell()
        except FileNotFoundError:
            chunk = ""

        if chunk:
            deadline = time.monotonic() + _FIRST_EVENT_TIMEOUT
            pending += chunk
            *complete, pending = pending.split("\n")
            for line in complete:
                if not line.strip():
                    continue
                try:
                    event = json.loads(line)
                except ValueError:
                    # Not a torn write -- a flushed line that is not JSON is a
                    # bug worth surfacing rather than skipping in silence.
                    yield {"kind": "error", "error": f"unreadable event: {line[:200]}"}
                    return
                yield event
                if event.get("kind") in ("run_finished", "error"):
                    return

        if time.monotonic() > deadline:
            yield {
                "kind": "error",
                "error": (
                    "no progress for 60 seconds and the run has not reported "
                    "finishing. It may still be running -- reload to read the "
                    "stored state, which is the record."
                ),
            }
            return
        time.sleep(_POLL_SECONDS)
