"""The six dispositions, as a state machine over `findings.state`.

THREE OF THE SIX REQUIRE AN ARGUMENT, AND THE REQUIREMENT IS THE POINT. Each
one is a predecessor failure written down:

- `reject` without a reason produces a decision ledger that cannot be
  calibrated against, and calibration is the only reason to record rejections.
- `defer` without a wake condition is how the predecessor lost deferred
  findings forever.
- `hand_off` without an assignee is this project's founding failure -- five
  correct recommendations, zero deployments, nobody named.

`needs_evidence` requires one too, for the same family of reason: "come back
with more" is not a specification, and the lens is being asked to spend money
against it.

`handed_off` and `deferred` are OPEN states. A finding assigned to a person is
not finished, it is waiting on one, and the whole design exists because
"assigned to a human" became the same silent black hole as "not found".
"""

from __future__ import annotations

import sqlite3
import uuid
from enum import StrEnum

from ..errors import WhetstoneError
from ..store.findings import FindingState


class DispositionError(WhetstoneError):
    """A disposition that cannot be applied, and why.

    A `WhetstoneError` so the CLI's existing handler renders it as a message
    rather than letting it out as a traceback.
    """


class Disposition(StrEnum):
    """What a human decides about a finding.

    Six, matching the design's section 4.4. There is no `close` and no `fixed`: the
    first is `reject` with a reason, and the second cannot be known until
    something verifies it, which is M1b-2's job.
    """

    verify = "verify"
    implement = "implement"
    hand_off = "hand_off"
    needs_evidence = "needs_evidence"
    defer = "defer"
    reject = "reject"

    @property
    def resulting_state(self) -> str:
        """The state this disposition moves a finding to.

        `needs_evidence` is the exception and is resolved by `apply`, which can
        see how many times this finding has already been sent back. It reports
        `queued` here because that is where the first ask lands.
        """
        return _RESULTING_STATE[self]


# Spelled through `FindingState` rather than as literals. The CLI's `--state`
# filter is typed on that enum, so a state named only here is one the store
# holds rows in and the CLI refuses as a typo.
_RESULTING_STATE = {
    Disposition.verify: str(FindingState.verified),
    Disposition.implement: str(FindingState.building),
    Disposition.hand_off: str(FindingState.handed_off),
    Disposition.needs_evidence: str(FindingState.queued),
    Disposition.defer: str(FindingState.deferred),
    Disposition.reject: str(FindingState.rejected),
}

# The argument each disposition cannot do without, by keyword.
_REQUIRED = {
    Disposition.hand_off: "assignee",
    Disposition.needs_evidence: "reason",
    Disposition.defer: "wake",
    Disposition.reject: "reason",
}

# `rejected` is the only state a finding does not come back from -- and even
# then the row stays, so the dedupe ledger keeps suppressing it.
#
# Everything else is OPEN, including `stalled`. A stalled finding is one the
# lens was asked twice about and could not evidence; that is a dead end, not a
# decision, and hiding it would lose the record that the tool was asked and
# came back empty.
TERMINAL: frozenset[str] = frozenset({str(FindingState.rejected)})
# Derived, not listed. A state added to `FindingState` and forgotten here would
# be invisible to every "what is still open" query written on top of these two
# names -- which is the black hole this milestone exists to close, reappearing
# one layer down.
OPEN: frozenset[str] = frozenset(str(s) for s in FindingState) - TERMINAL

# How many times one finding may be sent back to its lens before it stalls.
# One. The second ask is the last, because unbounded this is a loop: the lens
# is asked again, produces the same thing, and is asked again.
_MAX_EVIDENCE_ASKS = 1


def apply(
    conn: sqlite3.Connection,
    finding_id: str,
    disposition: Disposition,
    *,
    reason: str | None = None,
    wake: str | None = None,
    assignee: str | None = None,
    now: str,
) -> str:
    """Apply *disposition* to *finding_id*. Returns the new state.

    Refuses rather than defaulting, in three ways that each produced a real
    defect somewhere in this project's history: a missing finding raises
    instead of silently doing nothing (which reads to the caller as a recorded
    decision), a terminal state refuses every disposition (a rejection a later
    click can undo is not a rejection), and a required argument that is blank
    is treated as absent (a single space satisfies `if not reason` in exactly
    the codebases where the requirement was meant to bite).

    The findings row and the decisions row are written together. A ledger that
    records an attempt the store refused disagrees with the findings table
    about what happened, and the acceptance rate is computed from the ledger.
    """
    if not isinstance(disposition, Disposition):
        raise DispositionError(
            f"{disposition!r} is not a disposition. Valid: "
            f"{', '.join(d.value for d in Disposition)}."
        )

    row = conn.execute(
        "SELECT id, lens, state FROM findings WHERE id = ?", (finding_id,)
    ).fetchone()
    if row is None:
        raise DispositionError(
            f"no finding with id {finding_id!r}. Nothing was recorded -- run "
            "`whetstone findings` to see the ids this project has."
        )

    current = row["state"]
    if current in TERMINAL:
        raise DispositionError(
            f"finding {finding_id} is {current} and nothing moves it. A "
            "decision a later click can undo is not a decision, and the "
            "rejection is what suppresses this finding on every future run."
        )

    _require_argument(disposition, reason=reason, wake=wake, assignee=assignee)
    new_state = _resolve_state(conn, finding_id, disposition)

    # `grade` is deliberately not touched. A human decision is about what to
    # DO; it is not a re-judgement of the evidence, and overwriting the grade
    # would erase what the gate actually found.
    conn.execute(
        "UPDATE findings SET state = ?, updated_at = ? WHERE id = ?",
        (new_state, now, finding_id),
    )
    conn.execute(
        "INSERT INTO decisions (id, finding_id, lens, disposition, from_state, "
        "to_state, reason, wake, assignee, decided_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            uuid.uuid4().hex,
            finding_id,
            # Denormalised: the acceptance rate is per-lens, and a decision has
            # to stay answerable about which lens it judged even if the finding
            # row is later gone.
            row["lens"],
            str(disposition),
            current,
            new_state,
            reason,
            wake,
            assignee,
            now,
        ),
    )
    return new_state


def _require_argument(
    disposition: Disposition,
    *,
    reason: str | None,
    wake: str | None,
    assignee: str | None,
) -> None:
    """Refuse a disposition whose required argument is missing or blank.

    The message names the argument AND why it is required. "missing option" is
    a message a user works around; "reject needs a reason, because the ledger
    is what calibrates the lens" is one they answer.
    """
    required = _REQUIRED.get(disposition)
    if required is None:
        return
    value = {"reason": reason, "wake": wake, "assignee": assignee}[required]
    if value is not None and value.strip():
        return
    raise DispositionError(
        f"{disposition} needs {required}: {_WHY_REQUIRED[disposition]}"
    )


_WHY_REQUIRED = {
    Disposition.reject: (
        "a reason. The decision ledger is what calibrates the lens, and a "
        "rejection with no reason tells it nothing about what it got wrong."
    ),
    Disposition.defer: (
        "a wake date or condition. A deferral with neither is how the "
        "predecessor lost deferred findings forever."
    ),
    Disposition.hand_off: (
        "an assignee. Handing a finding to nobody in particular is the "
        "failure this project was built to fix: five correct "
        "recommendations, zero deployments."
    ),
    Disposition.needs_evidence: (
        "a reason naming what is missing. The lens is about to spend money "
        "against this instruction, and 'come back with more' is not one."
    ),
}


def _resolve_state(
    conn: sqlite3.Connection, finding_id: str, disposition: Disposition
) -> str:
    """`needs_evidence` returns to the queue once, then stalls.

    Counted over THIS finding's own decisions. Counting every
    `needs_evidence` row in the store would make the second finding in a
    project stall on its first ask.
    """
    if disposition is not Disposition.needs_evidence:
        return disposition.resulting_state

    asks = conn.execute(
        "SELECT COUNT(*) FROM decisions WHERE finding_id = ? AND disposition = ?",
        (finding_id, str(Disposition.needs_evidence)),
    ).fetchone()[0]
    return "queued" if asks < _MAX_EVIDENCE_ASKS else "stalled"
