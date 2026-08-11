"""Finding records: the durable form of a lens candidate."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from typing import Any

from ..lenses.base import Candidate


@dataclass(frozen=True)
class Finding:
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


def upsert(
    conn: sqlite3.Connection, candidate: Candidate, run_id: str, now: str
) -> bool:
    """Persist *candidate*. Returns True when it had not been seen before.

    A finding already in the table keeps its state — a rejection must never be
    undone by re-running. Only the evidence and last_seen_run are refreshed.
    """
    key = candidate.dedupe_key
    existing = conn.execute(
        "SELECT id FROM findings WHERE dedupe_key = ?", (key,)
    ).fetchone()

    if existing is not None:
        conn.execute(
            "UPDATE findings SET evidence_json = ?, last_seen_run = ?, "
            "updated_at = ? WHERE dedupe_key = ?",
            (candidate.evidence.to_json(), run_id, now, key),
        )
        return False

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
