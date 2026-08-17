"""The six dispositions, and the three that require an argument.

Every required argument here is a predecessor failure written down. `reject`
without a reason produces a ledger that cannot be calibrated against, which is
the whole purpose of recording rejections. `defer` without a wake condition is
how the predecessor lost deferred findings forever. `hand_off` without an
assignee is this project's founding failure: five correct recommendations, zero
deployments, nobody named.
"""

import sqlite3

import pytest

from whetstone.errors import WhetstoneError
from whetstone.grade import Grade
from whetstone.lenses.base import Candidate, Evidence, EvidenceKind, Severity
from whetstone.queue.dispositions import (
    OPEN,
    TERMINAL,
    Disposition,
    DispositionError,
    apply,
)
from whetstone.store.db import connect
from whetstone.store.findings import count_by_state, list_findings, upsert

NOW = "2026-08-17T10:00:00+00:00"
LATER = "2026-08-18T10:00:00+00:00"


def _candidate(subject: str = "orders.py:9", grade: Grade | None = Grade.A) -> Candidate:
    return Candidate(
        lens="code-defects",
        rule_id="defect",
        subject=subject,
        title="division by zero on an empty order",
        detail="d",
        severity=Severity.high,
        evidence=Evidence(EvidenceKind.repro, "s", {}),
        grade=grade,
        grade_reason="graded A: reproduced." if grade else None,
    )


@pytest.fixture
def _open_stores():
    """Every connection these tests open, closed at teardown.

    The helper below used to hand back an open `sqlite3.Connection` that only
    one test ever closed. On Windows an open SQLite handle can make `tmp_path`
    cleanup fail, and two of the four CI legs are Windows -- so the leak was a
    CI flake waiting for the directory to be big enough to matter.
    """
    opened: list = []
    yield opened
    for conn in opened:
        conn.close()


@pytest.fixture
def one_finding(_open_stores):
    def _make(tmp_path, subject: str = "orders.py:9"):
        conn = connect(tmp_path)
        _open_stores.append(conn)
        upsert(conn, _candidate(subject), "run-1", NOW)
        return conn, list_findings(conn)[0].id

    return _make


# --- the three required arguments --------------------------------------------


def test_reject_requires_a_reason(tmp_path, one_finding):
    conn, fid = one_finding(tmp_path)
    with pytest.raises(DispositionError, match="reason"):
        apply(conn, fid, Disposition.reject, now=NOW)


def test_defer_requires_a_wake_condition(tmp_path, one_finding):
    conn, fid = one_finding(tmp_path)
    with pytest.raises(DispositionError, match="wake"):
        apply(conn, fid, Disposition.defer, now=NOW)


def test_hand_off_requires_an_assignee(tmp_path, one_finding):
    conn, fid = one_finding(tmp_path)
    with pytest.raises(DispositionError, match="assignee"):
        apply(conn, fid, Disposition.hand_off, now=NOW)


def test_needs_evidence_requires_saying_what_is_missing(tmp_path, one_finding):
    """"Come back with more" is the instruction that produced nothing twice."""
    conn, fid = one_finding(tmp_path)
    with pytest.raises(DispositionError, match="reason"):
        apply(conn, fid, Disposition.needs_evidence, now=NOW)


@pytest.mark.parametrize(
    "disposition,kwargs,noun",
    [
        (Disposition.reject, {}, "reason"),
        (Disposition.defer, {}, "wake"),
        (Disposition.hand_off, {}, "assignee"),
        (Disposition.needs_evidence, {}, "reason"),
    ],
)
def test_the_refusal_reads_as_a_sentence_and_says_why(
    tmp_path, one_finding, disposition, kwargs, noun
):
    """The message is the whole mechanism -- "missing option" is a message a
    user works around, and one naming the argument AND the reason is one they
    answer. Interpolating the noun in front of a sentence that already opened
    with it printed "reject needs reason: a reason. The decision ledger...".
    """
    conn, fid = one_finding(tmp_path)
    with pytest.raises(DispositionError) as excinfo:
        apply(conn, fid, disposition, now=NOW, **kwargs)
    message = str(excinfo.value)

    assert noun in message
    # The exact opening, not merely "the noun is absent": interpolating the
    # noun in front of a sentence that already opens with it produced
    # "reject needs reason a reason. The decision ledger..." -- which contains
    # the noun, reads as broken English, and satisfies any looser assertion.
    assert message.startswith(f"{disposition} needs a"), message
    # A reason, not just a demand: every one of these explains what breaks.
    assert len(message.split()) > 12, message


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_a_blank_argument_is_not_an_argument(tmp_path, one_finding, blank):
    """A space satisfies `if not reason` in exactly the codebases where the
    requirement was meant to bite."""
    conn, fid = one_finding(tmp_path)
    with pytest.raises(DispositionError, match="reason"):
        apply(conn, fid, Disposition.reject, reason=blank, now=NOW)


# --- what each disposition resolves to ----------------------------------------


@pytest.mark.parametrize(
    "disposition,kwargs,expected",
    [
        (Disposition.verify, {}, "verified"),
        (Disposition.implement, {}, "building"),
        (Disposition.hand_off, {"assignee": "ritvik"}, "handed_off"),
        (Disposition.defer, {"wake": "2026-09-01"}, "deferred"),
        (Disposition.reject, {"reason": "intended behaviour"}, "rejected"),
        (Disposition.needs_evidence, {"reason": "no repro"}, "queued"),
    ],
)
def test_each_disposition_resolves_to_its_state(
    tmp_path, one_finding, disposition, kwargs, expected
):
    conn, fid = one_finding(tmp_path)
    assert apply(conn, fid, disposition, now=NOW, **kwargs) == expected
    assert list_findings(conn)[0].state == expected


# --- open is not terminal -----------------------------------------------------


def test_handed_off_is_open_not_terminal(tmp_path, one_finding):
    """The founding failure was "assigned to a human" becoming a black hole."""
    conn, fid = one_finding(tmp_path)
    apply(conn, fid, Disposition.hand_off, assignee="ritvik", now=NOW)
    assert "handed_off" in count_by_state(conn)
    assert "handed_off" not in TERMINAL
    assert "handed_off" in OPEN


def test_deferred_is_open_not_terminal(tmp_path, one_finding):
    conn, fid = one_finding(tmp_path)
    apply(conn, fid, Disposition.defer, wake="2026-09-01", now=NOW)
    assert "deferred" not in TERMINAL
    assert "deferred" in OPEN


def test_open_and_terminal_partition_every_state_a_disposition_can_produce(
    tmp_path, one_finding
):
    """A state in neither set is invisible to every "what is still open" query
    that will be written on top of these two names."""
    produced = {"queued", "stalled"} | {d.resulting_state for d in Disposition}
    assert produced <= (OPEN | TERMINAL)
    assert not (OPEN & TERMINAL)


def test_rejected_is_the_only_terminal_state():
    assert frozenset({"rejected"}) == TERMINAL


def test_every_state_a_disposition_produces_is_a_state_the_cli_can_filter():
    """`FindingState` exists so `findings --state` can reject a typo. A valid
    state missing from it is the same lie in reverse: the CLI refuses a state
    the store is holding rows in, and those rows are unreachable.

    Asserted against `FindingState` DIRECTLY, not against `OPEN | TERMINAL`.
    `OPEN` is derived as `frozenset(FindingState) - TERMINAL`, so the earlier
    version of this test -- `known == (OPEN | TERMINAL)` -- was true for any
    `TERMINAL` drawn from the enum and could not fail. It restated the
    derivation instead of the property.
    """
    from whetstone.store.findings import FindingState

    known = {str(s) for s in FindingState}
    produced = {d.resulting_state for d in Disposition} | {"stalled"}
    assert produced <= known, produced - known


# --- a decision outlives the run that produced the finding --------------------


def test_a_rejected_finding_survives_a_rerun(tmp_path, one_finding):
    """`upsert` never touches `state`; every disposition inherits that."""
    conn, fid = one_finding(tmp_path)
    apply(conn, fid, Disposition.reject, reason="intended behaviour", now=NOW)
    upsert(conn, _candidate(), "run-2", NOW)
    assert list_findings(conn)[0].state == "rejected"


def test_every_disposition_survives_a_rerun(tmp_path, one_finding):
    """Not just rejection. A finding handed to a person must not silently
    return to the queue because the next run found it again."""
    for i, (disposition, kwargs, expected) in enumerate(
        [
            (Disposition.verify, {}, "verified"),
            (Disposition.implement, {}, "building"),
            (Disposition.hand_off, {"assignee": "r"}, "handed_off"),
            (Disposition.defer, {"wake": "2026-09-01"}, "deferred"),
        ]
    ):
        conn, fid = one_finding(tmp_path / f"s{i}", subject=f"f{i}.py:1")
        apply(conn, fid, disposition, now=NOW, **kwargs)
        upsert(conn, _candidate(f"f{i}.py:1"), "run-2", LATER)
        assert list_findings(conn)[0].state == expected


# --- needs_evidence returns once, then stalls ---------------------------------


def test_needs_evidence_returns_once_then_stalls(tmp_path, one_finding):
    """Unbounded, this is a loop: the lens is asked again, produces the same
    thing, and is asked again. The second ask is the last one."""
    conn, fid = one_finding(tmp_path)
    assert apply(conn, fid, Disposition.needs_evidence, reason="no repro", now=NOW) == (
        "queued"
    )
    assert apply(
        conn, fid, Disposition.needs_evidence, reason="still none", now=LATER
    ) == "stalled"


def test_the_stall_counts_this_findings_own_asks_not_the_stores(tmp_path, one_finding):
    """Counting rows in `decisions` rather than rows for THIS finding makes the
    second finding in a project stall on its first ask."""
    conn, first = one_finding(tmp_path)
    apply(conn, first, Disposition.needs_evidence, reason="no repro", now=NOW)

    upsert(conn, _candidate("other.py:1"), "run-1", NOW)
    second = next(f.id for f in list_findings(conn) if f.subject == "other.py:1")
    assert apply(
        conn, second, Disposition.needs_evidence, reason="no repro", now=NOW
    ) == "queued"


# --- refusals -----------------------------------------------------------------


def test_an_unknown_transition_refuses_rather_than_defaulting(tmp_path, one_finding):
    conn, fid = one_finding(tmp_path)
    apply(conn, fid, Disposition.reject, reason="no", now=NOW)
    with pytest.raises(DispositionError, match="rejected"):
        apply(conn, fid, Disposition.verify, now=NOW)


def test_a_finding_that_does_not_exist_refuses(tmp_path, one_finding, _open_stores):
    """Silently doing nothing here reads to the caller as a recorded decision."""
    conn = connect(tmp_path)
    _open_stores.append(conn)
    with pytest.raises(DispositionError, match="no finding"):
        apply(conn, "nope", Disposition.verify, now=NOW)


@pytest.mark.parametrize("value", ["verify", "reject", None, 1, object()])
def test_something_that_is_not_a_disposition_refuses_and_lists_the_six(
    tmp_path, one_finding, value
):
    """The string `"verify"` is the likely caller mistake and is refused too.

    `Disposition` is a `StrEnum`, so `"verify" == Disposition.verify` is True
    and a laxer check would let a raw string through -- and then `"verrify"`
    reaches `_RESULTING_STATE` as a KeyError three frames down instead of a
    message naming the six valid values.
    """
    conn, fid = one_finding(tmp_path)
    with pytest.raises(DispositionError, match="not a disposition"):
        apply(conn, fid, value, now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0


def test_the_finding_and_the_decision_are_written_in_one_transaction(
    tmp_path, one_finding
):
    """`connect()` is autocommit, so these were two committed writes.

    The UPDATE landed first; a failing INSERT then left the finding moved with
    no decision recorded, and the acceptance rate is computed from the
    decisions table. The docstring on `apply` claimed the two were "written
    together" and nothing implemented it.

    Forced with a trigger rather than a mock. `sqlite3.Connection.execute` is
    read-only and cannot be monkeypatched, and a trigger is the truer injection
    anyway: the INSERT really is attempted and really does fail, inside the
    same connection and the same transaction the production path uses.
    """
    conn, fid = one_finding(tmp_path)
    conn.execute(
        "CREATE TRIGGER refuse_decisions BEFORE INSERT ON decisions "
        "BEGIN SELECT RAISE(ABORT, 'injected'); END"
    )
    try:
        with pytest.raises(sqlite3.IntegrityError):
            apply(conn, fid, Disposition.reject, reason="no", now=NOW)
    finally:
        conn.execute("DROP TRIGGER refuse_decisions")

    assert list_findings(conn)[0].state == "queued", (
        "the finding moved even though its decision was never recorded"
    )
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0


def test_disposition_error_is_a_whetstone_error(tmp_path, one_finding):
    """The CLI catches WhetstoneError; anything else reaches a user as a bare
    traceback."""
    assert issubclass(DispositionError, WhetstoneError)


# --- the ledger ---------------------------------------------------------------


def test_the_decision_is_recorded_with_everything_it_needs_to_be_audited(tmp_path, one_finding):
    conn, fid = one_finding(tmp_path)
    apply(conn, fid, Disposition.hand_off, assignee="ritvik", now=NOW)
    row = conn.execute("SELECT * FROM decisions").fetchone()

    assert row["finding_id"] == fid
    assert row["disposition"] == "hand_off"
    assert row["from_state"] == "queued"
    assert row["to_state"] == "handed_off"
    assert row["assignee"] == "ritvik"
    assert row["decided_at"] == NOW
    # Denormalised on purpose: the acceptance rate is per-lens, and a decision
    # has to stay answerable even if the finding row is later gone.
    assert row["lens"] == "code-defects"


def test_every_disposition_writes_exactly_one_decision_row(tmp_path, one_finding):
    conn, fid = one_finding(tmp_path)
    apply(conn, fid, Disposition.needs_evidence, reason="a", now=NOW)
    apply(conn, fid, Disposition.needs_evidence, reason="b", now=LATER)
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 2


def test_a_refused_disposition_writes_no_decision(tmp_path, one_finding):
    """A ledger that records attempts the store rejected is a ledger that
    disagrees with the findings table about what happened."""
    conn, fid = one_finding(tmp_path)
    with pytest.raises(DispositionError):
        apply(conn, fid, Disposition.reject, now=NOW)
    assert conn.execute("SELECT COUNT(*) FROM decisions").fetchone()[0] == 0
    assert list_findings(conn)[0].state == "queued"


@pytest.mark.parametrize(
    "column", ["finding_id", "lens", "disposition", "from_state", "to_state", "decided_at"]
)
def test_a_decision_cannot_be_written_without_the_columns_it_is_audited_by(
    tmp_path, one_finding, column
):
    """The schema argues these are required; this is the assertion for it.

    `apply` cannot produce a NULL in any of them today, so the constraint is
    reachable only by a future caller -- which is exactly when a comment
    stating a guarantee stops being worth anything. A decision with no lens is
    one the acceptance rate silently drops.
    """
    import sqlite3

    conn, fid = one_finding(tmp_path)
    values = {
        "id": "d1",
        "finding_id": fid,
        "lens": "code-defects",
        "disposition": "verify",
        "from_state": "queued",
        "to_state": "verified",
        "decided_at": NOW,
    }
    values[column] = None
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            f"INSERT INTO decisions ({', '.join(values)}) "
            f"VALUES ({', '.join('?' * len(values))})",
            tuple(values.values()),
        )


def test_a_decision_cannot_reference_a_finding_that_does_not_exist(tmp_path, one_finding):
    """`PRAGMA foreign_keys=ON` is set in `connect`; this is what it buys.

    Without it the ledger accumulates decisions about nothing, and every rate
    computed from it is divided by a denominator that includes them.
    """
    import sqlite3

    conn, _ = one_finding(tmp_path)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "INSERT INTO decisions (id, finding_id, lens, disposition, from_state, "
            "to_state, decided_at) VALUES ('d1', 'ghost', 'l', 'verify', 'queued', "
            "'verified', ?)",
            (NOW,),
        )


def test_the_grade_is_untouched_by_a_disposition(tmp_path, one_finding):
    """A human decision is about what to DO. It is not a re-judgement of the
    evidence, and overwriting the grade would erase what the gate found."""
    conn, fid = one_finding(tmp_path)
    apply(conn, fid, Disposition.reject, reason="wont fix", now=NOW)
    assert list_findings(conn)[0].grade == "A"
