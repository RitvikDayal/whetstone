"""SQLite connection and schema."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1

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
    """Open (creating if needed) the database under *state_root*."""
    state_root.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(state_root / "whetstone.db", isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(_SCHEMA)
    conn.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
    return conn
