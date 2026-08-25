"""SQLite connection and schema."""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path

from ..errors import SchemaVersionError, StoreError

SCHEMA_VERSION = 2

_SCHEMA = """
CREATE TABLE IF NOT EXISTS findings (
    id             TEXT PRIMARY KEY,
    dedupe_key     TEXT NOT NULL UNIQUE,
    lens           TEXT NOT NULL,
    rule_id        TEXT NOT NULL,
    subject        TEXT NOT NULL,
    title          TEXT NOT NULL,
    detail         TEXT NOT NULL,
    severity       TEXT NOT NULL,
    evidence_json  TEXT NOT NULL,
    state          TEXT NOT NULL DEFAULT 'queued',
    -- Nullable on purpose: a lens that does not grade leaves both NULL, which
    -- is not the same as grade D. Reading absent as killed would report a real
    -- defect as dismissed by a stage that never looked at it.
    grade          TEXT,
    grade_reason   TEXT,
    first_seen_run TEXT NOT NULL,
    last_seen_run  TEXT NOT NULL,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_findings_state ON findings(state);
CREATE INDEX IF NOT EXISTS idx_findings_lens ON findings(lens);

-- Every human decision about a finding, append-only. The findings table holds
-- the CURRENT state; this holds how it got there, which is the only thing an
-- acceptance rate can be computed from.
--
-- `lens` is denormalised off the finding on purpose: the rate is per-lens, and
-- a decision must stay answerable about which lens it judged even after the
-- finding row is gone. `reason`, `wake` and `assignee` are nullable because
-- only some dispositions require one -- which of them is enforced in
-- `queue/dispositions.py`, not here, so the message can say why.
CREATE TABLE IF NOT EXISTS decisions (
    id          TEXT PRIMARY KEY,
    finding_id  TEXT NOT NULL,
    lens        TEXT NOT NULL,
    disposition TEXT NOT NULL,
    from_state  TEXT NOT NULL,
    to_state    TEXT NOT NULL,
    reason      TEXT,
    wake        TEXT,
    assignee    TEXT,
    decided_at  TEXT NOT NULL,
    FOREIGN KEY (finding_id) REFERENCES findings(id)
);

CREATE INDEX IF NOT EXISTS idx_decisions_finding ON decisions(finding_id);
CREATE INDEX IF NOT EXISTS idx_decisions_lens ON decisions(lens);

CREATE TABLE IF NOT EXISTS runs (
    id           TEXT PRIMARY KEY,
    tier         TEXT NOT NULL,
    scope_mode   TEXT NOT NULL,
    file_count   INTEGER NOT NULL DEFAULT 0,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT NOT NULL,
    skipped_json TEXT NOT NULL DEFAULT '[]'
);
"""


# How long to keep retrying the switch into WAL. Small, because the contention
# it covers lasts microseconds -- see `_enable_wal`.
_WAL_RETRY_SECONDS = 5.0
_WAL_RETRY_PAUSE = 0.02


# SQLite's own wording for "somebody else holds it". Matched on the message
# because `sqlite3` does not expose the extended result code on the exception
# in a form worth depending on -- `sqlite3_errorcode` is not surfaced, and
# `OperationalError` covers everything from a lock to a corrupt file.
_LOCK_MESSAGES = ("database is locked", "database table is locked",
                  "locking protocol")


def _is_lock_error(exc: sqlite3.OperationalError) -> bool:
    lowered = str(exc).lower()
    return any(message in lowered for message in _LOCK_MESSAGES)


def _enable_wal(conn: sqlite3.Connection, db_path: Path) -> None:
    """Put the database in WAL mode, tolerating a concurrent first open.

    THE BUSY TIMEOUT DOES NOT COVER THIS. Switching journal modes takes an
    EXCLUSIVE lock, and SQLite returns SQLITE_BUSY for it immediately rather
    than invoking the busy handler -- so the 30-second timeout on the
    connection above, which covers every ordinary write, does nothing here.

    MEASURED, not theorised: three threads opening a brand-new database at once
    failed with `OperationalError: database is locked` in roughly half of
    twenty-five trials. That is not an exotic case -- the control plane's first
    page load fires `/api/findings`, `/api/trust` and `/api/costs` in parallel,
    each opening its own connection, so a fresh project met this on its first
    screen about half the time and got a 500.

    READ BEFORE WRITE, which is what makes the retry converge. `journal_mode`
    is a persistent property of the FILE, so once any connection has set it,
    every later one only has to read it back -- and reading takes no exclusive
    lock. The race window is therefore the first few milliseconds of a
    database's life, and a loser of that race finds the winner's work done.
    """
    if _journal_mode(conn) == "wal":
        return

    deadline = time.monotonic() + _WAL_RETRY_SECONDS
    while True:
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.OperationalError as exc:
            # ONLY A LOCK. `OperationalError` also covers "file is not a
            # database", "disk I/O error" and "attempt to write a readonly
            # database" -- none of which another five seconds of retrying will
            # fix, and every one of which would have been re-reported as a WAL
            # contention problem, sending the reader to look for a second
            # Whetstone process that does not exist.
            if not _is_lock_error(exc):
                raise
            # Somebody else is mid-switch. Re-READ rather than assuming the
            # attempt failed: the exception may mean they already succeeded.
            if _journal_mode(conn) == "wal":
                return
            if time.monotonic() >= deadline:
                raise StoreError(
                    f"{db_path} could not be put into WAL mode within "
                    f"{_WAL_RETRY_SECONDS:.0f}s because another process holds "
                    "it. WAL is not optional here -- `paths.py` refuses "
                    "cloud-synced state directories precisely because the -wal "
                    "and -shm sidecars must stay consistent with the main "
                    "file, and running without it would silently drop that "
                    "guarantee. Close other Whetstone processes and retry."
                ) from None
            time.sleep(_WAL_RETRY_PAUSE)
            continue
        if _journal_mode(conn) == "wal":
            return
        # `PRAGMA journal_mode=WAL` can return the OLD mode without raising
        # when it could not take the lock. Reading it back is the only honest
        # confirmation that the mode actually changed.
        if time.monotonic() >= deadline:
            raise StoreError(
                f"{db_path} reports journal_mode "
                f"{_journal_mode(conn)!r} after being asked for WAL. See "
                "above for why WAL is required."
            )
        time.sleep(_WAL_RETRY_PAUSE)


def _journal_mode(conn: sqlite3.Connection) -> str:
    """The current mode, or "" when another connection is mid-switch.

    THE READ CAN BE LOCKED OUT TOO. `PRAGMA journal_mode` is a read, but a
    connection changing the mode holds an exclusive lock for the duration, and
    a reader that arrives inside that window gets SQLITE_BUSY. An unhandled
    raise here would escape the retry loop that calls it -- the loop exists
    precisely because somebody else is mid-switch, so the read failing is the
    expected shape of that, not an exception to it.

    "" rather than a raise, so the caller retries. A non-lock error still
    propagates: see `_is_lock_error`.
    """
    try:
        row = conn.execute("PRAGMA journal_mode").fetchone()
    except sqlite3.OperationalError as exc:
        if not _is_lock_error(exc):
            raise
        return ""
    return str(row[0]).lower() if row else ""


def connect(state_root: Path) -> sqlite3.Connection:
    """Open (creating if needed) the database under *state_root*.

    Raises SchemaVersionError if the database was stamped by a different
    schema version. Zero means a fresh database and is stamped with
    SCHEMA_VERSION; there is no migration path yet, so any other mismatch
    is refused rather than silently restamped.
    """
    state_root.mkdir(parents=True, exist_ok=True)
    db_path = state_root / "whetstone.db"
    # timeout raises the busy-wait above sqlite3's 5s default. Two Whetstone
    # invocations against one project are not a documented workflow, but two
    # terminals are one keystroke apart.
    conn = sqlite3.connect(db_path, isolation_level=None, timeout=30.0)
    conn.row_factory = sqlite3.Row
    _enable_wal(conn, db_path)
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")

    stamped_version = conn.execute("PRAGMA user_version").fetchone()[0]
    if stamped_version != 0 and stamped_version != SCHEMA_VERSION:
        conn.close()
        raise SchemaVersionError(
            f"{db_path} was written by Whetstone schema version "
            f"{stamped_version}, but this build expects {SCHEMA_VERSION}. "
            "There is no migration path yet, so the only fix is to delete "
            f"{db_path} and run whetstone again -- which discards recorded "
            "findings AND any decisions made about them. Refusing rather "
            "than reading it, because a column this build expects and that "
            "file does not have is a value silently read as absent."
        )

    conn.executescript(_SCHEMA)
    if stamped_version == 0:
        conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    return conn
