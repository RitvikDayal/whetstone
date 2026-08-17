"""SQLite connection and schema."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from ..errors import SchemaVersionError

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
    conn.execute("PRAGMA journal_mode=WAL")
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
