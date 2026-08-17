"""The decision ledger: every human judgement, and the rate computed from it.

THE ONLY WRITER OF THE `decisions` TABLE. `queue/dispositions.py` wrote the
INSERT inline while nothing else existed; leaving it there once this module
exists gives one table two writers, and the second one written is the one that
forgets a column. `upsert` and `_refresh` disagreeing about what a refresh
updates already cost this project a run, and that was two writers of one row.

THE RATE NEVER TRAVELS WITHOUT ITS SAMPLE SIZE. A rate computed from two
decisions is not a rate, and a bare float invites a caller to treat it as one.
`None` rather than `0.0` when there are no decisions at all -- `0.0` is the
claim that everything was rejected, which is the opposite claim, and nothing
downstream can tell them apart from the number alone.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from ..errors import WhetstoneError

# Which dispositions are a judgement about whether the finding was REAL.
#
# `hand_off` is an acceptance: the human agreed it was real and named someone.
# Counting it as a rejection makes the tool look worse than it is, which is the
# direction that gets a lens switched off.
#
# `defer` and `needs_evidence` are neither, and are excluded from the numerator
# AND the denominator. They are "not now" and "not yet". Counted as rejections,
# a busy week reads as a quality collapse and demotes a lens that did nothing
# wrong; counted as acceptances, a queue nobody triaged reads as a perfect
# record.
#
# EXPORTED, not private. `autonomy.py` needs the same classification for its
# trailing window, and it had its own copy with a comment promising to keep
# them in step -- which is a promise, not a mechanism. One definition means
# `acceptance_rate` and `_trailing_collapse` cannot come to disagree about what
# a decision means.
ACCEPTANCES = frozenset({"verify", "implement", "hand_off"})
REJECTIONS = frozenset({"reject"})
COUNTED = ACCEPTANCES | REJECTIONS


@dataclass(frozen=True)
class Decision:
    """One row of the ledger, as written. Append-only; nothing updates these."""

    id: str
    finding_id: str
    lens: str
    disposition: str
    from_state: str
    to_state: str
    reason: str | None
    wake: str | None
    assignee: str | None
    decided_at: str


class DecisionError(WhetstoneError):
    """A decision that cannot be recorded, and why."""


def _canonical_time(value: str) -> str:
    """*value* as a UTC ISO-8601 string, or refuse it.

    `decided_at` is TEXT and `decisions_for` orders on it lexically, which is
    only chronological if every value is in the same offset. `+05:30`, `Z` and
    `+00:00` all sort against each other wrongly, and `_trailing_collapse`
    reads the TAIL of that ordering -- so a mixed-offset ledger silently
    demotes on the wrong ten decisions.

    Normalised at the write boundary rather than fixed in the query, because
    the query cannot repair what was stored. A naive timestamp is REFUSED
    rather than assumed UTC: assuming is a guess about a value that decides
    which decisions count, and the caller knows what it meant.
    """
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise DecisionError(
            f"decided_at {value!r} is not an ISO-8601 timestamp. The ledger is "
            "ordered by it, and the trailing window used for demotion reads "
            "the end of that order."
        ) from exc
    if parsed.tzinfo is None:
        raise DecisionError(
            f"decided_at {value!r} has no timezone. Two decisions recorded in "
            "different offsets would sort against each other wrongly, and "
            "nothing downstream could tell."
        )
    return parsed.astimezone(UTC).isoformat()


def record(
    conn: sqlite3.Connection,
    *,
    finding_id: str,
    lens: str,
    disposition: str,
    from_state: str,
    to_state: str,
    reason: str | None,
    wake: str | None,
    assignee: str | None,
    now: str,
) -> None:
    """Append one decision. Keyword-only, and every column is named.

    Nothing is optional-with-a-default here on purpose: a recorder that lets a
    caller omit `from_state` is one that will be called without it, and the
    column is NOT NULL because a decision that cannot say what it changed is
    not auditable.
    """
    conn.execute(
        "INSERT INTO decisions (id, finding_id, lens, disposition, from_state, "
        "to_state, reason, wake, assignee, decided_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            uuid.uuid4().hex,
            finding_id,
            lens,
            disposition,
            from_state,
            to_state,
            reason,
            wake,
            assignee,
            _canonical_time(now),
        ),
    )


def decisions_for(
    conn: sqlite3.Connection,
    *,
    lens: str | None = None,
    finding_id: str | None = None,
) -> list[Decision]:
    """The ledger, oldest first.

    KEYWORD-ONLY. Both filters are `str | None` and adjacent, so a positional
    swap -- or a finding id passed where a lens goes -- returns an empty list
    rather than raising. `autonomy.py` reads an empty ledger as "no decisions
    recorded" and produces probation with a confident explanation, which is a
    silent wrong answer with a sentence attached.

    Oldest first because this is a history: the order decisions were made in is
    the thing being read, and `autonomy.py` takes a TRAILING window off the end
    of it.
    """
    clauses: list[str] = []
    params: list[str] = []
    if lens is not None:
        clauses.append("lens = ?")
        params.append(lens)
    if finding_id is not None:
        clauses.append("finding_id = ?")
        params.append(finding_id)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        "SELECT * FROM decisions" + where + " ORDER BY decided_at ASC, rowid ASC",
        params,
    ).fetchall()
    return [
        Decision(
            id=row["id"],
            finding_id=row["finding_id"],
            lens=row["lens"],
            disposition=row["disposition"],
            from_state=row["from_state"],
            to_state=row["to_state"],
            reason=row["reason"],
            wake=row["wake"],
            assignee=row["assignee"],
            decided_at=row["decided_at"],
        )
        for row in rows
    ]


def acceptance_rate(
    conn: sqlite3.Connection, lens: str
) -> tuple[float | None, int]:
    """(rate, sample size) for *lens*. A FRACTION, not a percentage.

    0.6 and 60 are both plausible readings of "sixty percent" and only one is
    what `autonomy.py`'s thresholds compare against, so the unit is stated here
    and asserted in the tests.

    Every decision row counts, including a reversal. A finding accepted and
    later rejected contributes both -- deliberately. A rate that keeps only the
    latest decision per finding is calibrating against a tidied story rather
    than the record, and a reversal is exactly the signal that the lens
    produced something that looked right and was not.
    """
    counted = [
        d for d in decisions_for(conn, lens=lens) if d.disposition in COUNTED
    ]
    if not counted:
        return None, 0
    accepted = sum(1 for d in counted if d.disposition in ACCEPTANCES)
    return accepted / len(counted), len(counted)
