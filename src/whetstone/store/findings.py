"""Finding records: the durable form of a lens candidate."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from ..errors import StoreError
from ..lenses.base import Candidate


class FindingState(StrEnum):
    """The states a stored finding can be in.

    Seven, and every one of them is produced by something. M0 shipped two
    because M0 had no way to move a finding; `queue/dispositions.py` now
    produces the other five, and a state the store holds rows in but this enum
    does not name is a state `findings --state` refuses as a typo -- the same
    lie as the untyped version, running the other way.

    The enum exists so `findings --state` can reject a typo. Untyped, the CLI
    answered `--state bogus`, `--state Queued` and `--state ""` with "No
    findings in state 'X'." and exit 0 -- indistinguishable from a valid state
    that is genuinely empty, which is the same lie as a clean report over a
    run that checked nothing.

    There is still no `fixed`: nothing can verify a fix until M1b-2, and a
    vocabulary the tool cannot honour is worse than a missing word.
    """

    queued = "queued"
    verified = "verified"
    building = "building"
    handed_off = "handed_off"
    deferred = "deferred"
    stalled = "stalled"
    rejected = "rejected"


@dataclass(frozen=True)
class Finding:
    """The stored form of a Candidate.

    `evidence` is a plain dict decoded from the stored JSON, not the
    `Evidence` dataclass `Candidate` carries -- reaching for `.evidence.kind`
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
    # `None` means the lens did not grade this finding, NOT that it graded it
    # badly. Every reader has to keep the two apart.
    grade: str | None
    grade_reason: str | None
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
        grade=row["grade"],
        grade_reason=row["grade_reason"],
        first_seen_run=row["first_seen_run"],
        last_seen_run=row["last_seen_run"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )


def _grade_text(candidate: Candidate) -> str | None:
    """`str(grade)` only when there is one -- `str(None)` is the string 'None'.

    Issue #9 verbatim, and the reason it is a function rather than an inline
    conditional repeated at both write sites: the INSERT and the UPDATE must
    not be able to disagree about how absence is spelled.
    """
    return None if candidate.grade is None else str(candidate.grade)


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
        "severity = ?, grade = ?, grade_reason = ?, last_seen_run = ?, "
        "updated_at = ? WHERE dedupe_key = ?",
        (
            candidate.evidence.to_json(),
            candidate.title,
            candidate.detail,
            str(candidate.severity),
            # Written unconditionally, including back to NULL. The grade
            # describes the evidence THIS run gathered; a run that did not
            # grade the finding has not established that yesterday's grade
            # still holds, and presenting a stale verdict as a current one is
            # the failure the whole gate exists to prevent.
            _grade_text(candidate),
            candidate.grade_reason,
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
    `state` is never touched here -- a finding already in the table keeps its
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
            "detail, severity, evidence_json, state, grade, grade_reason, "
            "first_seen_run, last_seen_run, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'queued', ?, ?, ?, ?, ?, ?)",
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
                _grade_text(candidate),
                candidate.grade_reason,
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
    grade: str | None = None,
) -> list[Finding]:
    """Stored findings, filtered. `None` for a filter means "do not filter".

    `grade=None` therefore returns UNGRADED rows too -- it is not a filter for
    "has no grade". Nothing needs that yet, and spelling it as `None` would
    make the absent-filter case unreachable.
    """
    clauses: list[str] = []
    params: list[str] = []
    if state is not None:
        clauses.append("state = ?")
        params.append(state)
    if lens is not None:
        clauses.append("lens = ?")
        params.append(lens)
    if grade is not None:
        clauses.append("grade = ?")
        params.append(grade)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    # GRADE FIRST, severity second. Severity is what the model claimed about
    # its own finding; grade is what survived the gate, and the gate is the
    # only thing here that ran. Under severity-first a critical the falsifier
    # killed outranked a low it confirmed -- the model's opinion sorting above
    # the evidence, on the one surface a user reads.
    #
    # An ABSENT grade ranks between B and C, not last. `hygiene` does not grade
    # at all, and its findings are measured facts -- a CVE with an ID, a
    # coverage number below a floor. Sorting them below D would bury the most
    # reliable output the tool has underneath the output it just refuted, and
    # sorting them above A would let a coverage warning outrank a proven crash.
    # Both failures are avoided by putting "no verdict" in the middle.
    #
    # Severity is stored as text, so alphabetical ordering is meaningless
    # ("medium" would outrank "critical"). Both ranks are explicit.
    #
    # `id ASC` LAST, and it is not decoration: without it this ordering is not
    # total. `subject` is a file path and is routinely shared -- `code-defects`
    # hunts several candidates out of one file, so two findings at the same
    # grade and severity in the same file tie completely, and SQLite is then
    # free to return them in whatever order the query plan happens to produce.
    # Two callers issuing the same query can therefore get different orders,
    # which is exactly the silent two-surface disagreement the control plane
    # has to be able to test for. `get_last_run` learned the same lesson from a
    # measured tie on `started_at` and added a rowid tiebreaker; this is that
    # fix, applied where the ties are common rather than rare.
    rows = conn.execute(
        "SELECT * FROM findings"
        + where
        + " ORDER BY CASE grade"
        "   WHEN 'A' THEN 0 WHEN 'B' THEN 1"
        "   WHEN 'C' THEN 3 WHEN 'D' THEN 4 ELSE 2 END,"
        " CASE severity"
        "   WHEN 'critical' THEN 0 WHEN 'high' THEN 1"
        "   WHEN 'medium' THEN 2 ELSE 3 END, subject ASC, id ASC",
        params,
    ).fetchall()
    return [_row_to_finding(row) for row in rows]


def count_by_state(conn: sqlite3.Connection) -> dict[str, int]:
    rows = conn.execute(
        "SELECT state, COUNT(*) AS n FROM findings GROUP BY state"
    ).fetchall()
    return {row["state"]: row["n"] for row in rows}
