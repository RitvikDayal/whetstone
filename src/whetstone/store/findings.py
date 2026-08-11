"""Finding records: the durable form of a lens candidate."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from ..errors import StoreError
from ..lenses.base import Candidate


@dataclass(frozen=True)
class Finding:
    """The stored form of a Candidate.

    `evidence` is a plain dict decoded from the stored JSON, not the
    `Evidence` dataclass `Candidate` carries — reaching for `.evidence.kind`
    here raises `AttributeError`; use `.evidence["kind"]`.
    """

    id: str
    dedupe_key: str
    lens: str
    rule_id: str
    subject: str
    title: str
    detail: str
    severity: str
    evidence: dict[str, Any]
    state: str
    first_seen_run: str
    last_seen_run: str
    created_at: str
    updated_at: str


def _row_to_finding(row: sqlite3.Row) -> Finding:
    return Finding(
        id=row["id"],
        dedupe_key=row["dedupe_key"],
        lens=row["lens"],
        rule_id=row["rule_id"],
        subject=row["subject"],
        title=row["title"],
        detail=row["detail"],
        severity=row["severity"],
        evidence=json.loads(row["evidence_json"]),
        state=row["state"],
        first_seen_run=row["first_seen_run"],
        last_seen_run=row["last_seen_run"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _existing_id(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute(
        "SELECT id FROM findings WHERE dedupe_key = ?", (key,)
    ).fetchone()
    return row["id"] if row is not None else None


def _refresh(
    conn: sqlite3.Connection, candidate: Candidate, run_id: str, now: str, key: str
) -> None:
    """Update the stored row for *key*. Exactly one row must match.

    The backstop for the narrowed IntegrityError handler below. Both callers
    have already established that the row exists, so a zero-row UPDATE means the
    candidate has been dropped and the caller is about to report it as seen --
    the silent-loss failure this store must never produce.
    """
    cursor = conn.execute(
        "UPDATE findings SET evidence_json = ?, title = ?, detail = ?, "
        "severity = ?, last_seen_run = ?, updated_at = ? WHERE dedupe_key = ?",
        (
            candidate.evidence.to_json(),
            candidate.title,
            candidate.detail,
            str(candidate.severity),
            run_id,
            now,
            key,
        ),
    )
    if cursor.rowcount != 1:
        raise StoreError(
            f"refreshing finding {key} updated {cursor.rowcount} rows, expected "
            "exactly 1. The row was there when it was checked for and is not "
            "there now, so this candidate would have been silently discarded."
        )


def upsert(
    conn: sqlite3.Connection, candidate: Candidate, run_id: str, now: str
) -> bool:
    """Persist *candidate*. Returns True when it had not been seen before.

    Wording and severity are refreshed on every re-run: `Candidate.dedupe_key`
    deliberately excludes title, detail, and severity so a reworded or
    re-scored candidate is still recognised as the same finding, and that is
    only useful if the new wording and score then reach the stored row.
    `state` is never touched here — a finding already in the table keeps its
    state, so a rejection can never be undone by re-running.

    The existence check and the insert are two separate statements, so two
    callers can race: both miss the SELECT, then both attempt the INSERT. The
    loser's INSERT hits the UNIQUE constraint on dedupe_key; that is caught
    here, treated as "the row exists after all", and handled via the same
    refresh path used by the normal update branch.
    """
    key = candidate.dedupe_key

    if _existing_id(conn, key) is not None:
        _refresh(conn, candidate, run_id, now, key)
        return False

    try:
        conn.execute(
            "INSERT INTO findings (id, dedupe_key, lens, rule_id, subject, title, "
            "detail, severity, evidence_json, state, first_seen_run, last_seen_run, "
            "created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?)",
            (
                uuid.uuid4().hex,
                key,
                candidate.lens,
                candidate.rule_id,
                candidate.subject,
                candidate.title,
                candidate.detail,
                str(candidate.severity),
                candidate.evidence.to_json(),
                run_id,
                run_id,
                now,
                now,
            ),
        )
    except sqlite3.IntegrityError as exc:
        # Only the dedupe-key conflict means "someone else inserted this row".
        # Every NOT NULL violation is an IntegrityError too, and `Candidate` is
        # frozen but unvalidated, so a lens returning None for `detail` used to
        # land here, be reported as already seen, and vanish. Both the current
        # sqlite phrasing ("UNIQUE constraint failed: findings.dedupe_key") and
        # the legacy one ("column dedupe_key is not unique") are accepted.
        message = str(exc)
        if "dedupe_key" not in message or "UNIQUE" not in message.upper():
            raise
        _refresh(conn, candidate, run_id, now, key)
        return False

    return True


def list_findings(
    conn: sqlite3.Connection,
    *,
    state: str | None = None,
    lens: str | None = None,
) -> list[Finding]:
    clauses: list[str] = []
    params: list[str] = []
    if state is not None:
        clauses.append("state = ?")
        params.append(state)
    if lens is not None:
        clauses.append("lens = ?")
        params.append(lens)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    # Severity is stored as text, so alphabetical ordering is meaningless
    # ("medium" would outrank "critical"). Rank explicitly.
    rows = conn.execute(
        "SELECT * FROM findings"
        + where
        + " ORDER BY CASE severity"
        "   WHEN 'critical' THEN 0 WHEN 'high' THEN 1"
        "   WHEN 'medium' THEN 2 ELSE 3 END, subject ASC",
        params,
    ).fetchall()
    return [_row_to_finding(row) for row in rows]


def count_by_state(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT state, COUNT(*) AS n FROM findings GROUP BY state"
    ).fetchall()
    return {row["state"]: row["n"] for row in rows}
