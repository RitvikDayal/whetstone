import sqlite3
from dataclasses import replace

import pytest

from whetstone.errors import SchemaVersionError, StoreError
from whetstone.grade import Grade
from whetstone.lenses.base import Candidate, Evidence, EvidenceKind, Severity
from whetstone.store import db as db_module
from whetstone.store.db import SCHEMA_VERSION, connect
from whetstone.store.findings import count_by_state, list_findings, upsert

NOW = "2026-08-10T10:00:00+00:00"
LATER = "2026-08-10T11:00:00+00:00"


def _candidate(
    subject: str = "requests",
    rule_id: str = "CVE-2026-1",
    *,
    grade: Grade | None = None,
    grade_reason: str | None = None,
) -> Candidate:
    return Candidate(
        lens="hygiene",
        rule_id=rule_id,
        subject=subject,
        title=f"{subject} has a known vulnerability",
        detail="Upgrade to a patched release.",
        severity=Severity.high,
        evidence=Evidence(
            kind=EvidenceKind.metric,
            summary="pip-audit reported 1 advisory",
            data={"advisory": rule_id, "package": subject},
        ),
        grade=grade,
        grade_reason=grade_reason,
    )


def test_connect_sets_wal_and_schema_version(tmp_path):
    conn = connect(tmp_path)
    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    conn.close()


def test_connect_is_idempotent(tmp_path):
    connect(tmp_path).close()
    conn = connect(tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0] == 0
    conn.close()


def test_upsert_reports_new_then_seen(tmp_path):
    conn = connect(tmp_path)
    assert upsert(conn, _candidate(), "run-1", NOW) is True
    assert upsert(conn, _candidate(), "run-2", NOW) is False
    rows = list_findings(conn)
    assert len(rows) == 1
    assert rows[0].first_seen_run == "run-1"
    assert rows[0].last_seen_run == "run-2"
    conn.close()


def test_distinct_subjects_are_distinct_findings(tmp_path):
    conn = connect(tmp_path)
    upsert(conn, _candidate("requests"), "run-1", NOW)
    upsert(conn, _candidate("urllib3"), "run-1", NOW)
    assert len(list_findings(conn)) == 2
    conn.close()


def test_grade_is_persisted_and_refreshed(tmp_path):
    conn = connect(tmp_path)
    upsert(
        conn,
        _candidate(grade=Grade.B, grade_reason="no runnable artifact"),
        "run-1",
        NOW,
    )
    assert list_findings(conn)[0].grade == "B"

    upsert(
        conn,
        _candidate(grade=Grade.A, grade_reason="reproduced and survived"),
        "run-2",
        NOW,
    )
    row = list_findings(conn)[0]
    assert row.grade == "A", "a re-grade must reach the row, like title and severity do"
    assert "survived" in row.grade_reason
    conn.close()


def test_a_regrade_does_not_resurrect_a_rejection(tmp_path):
    conn = connect(tmp_path)
    upsert(
        conn,
        _candidate(grade=Grade.D, grade_reason="the falsifier killed it"),
        "run-1",
        NOW,
    )
    conn.execute("UPDATE findings SET state = 'rejected'")
    upsert(conn, _candidate(grade=Grade.A, grade_reason="reproduced"), "run-2", NOW)
    row = list_findings(conn)[0]
    assert row.grade == "A"
    assert row.state == "rejected", "the grade may change; the human decision may not"
    conn.close()


def test_findings_without_a_grade_are_not_claimed_to_have_one(tmp_path):
    conn = connect(tmp_path)
    upsert(conn, _candidate(), "run-1", NOW)
    row = list_findings(conn)[0]
    assert row.grade is None
    assert row.grade_reason is None
    conn.close()


def test_an_ungraded_rerun_clears_the_grade_rather_than_keeping_a_stale_one(tmp_path):
    """The grade describes the evidence THIS run gathered.

    A run that did not grade the finding has not established that yesterday's
    grade still holds, so keeping it would present a stale verdict as a current
    one. The pack does not emit an ungraded candidate today -- an unchallenged
    one is skipped by name rather than recorded -- so this asserts the rule
    rather than a path in use, and it is the same rule title and severity
    already follow.
    """
    conn = connect(tmp_path)
    upsert(conn, _candidate(grade=Grade.A, grade_reason="reproduced"), "run-1", NOW)
    upsert(conn, _candidate(), "run-2", NOW)
    row = list_findings(conn)[0]
    assert row.grade is None
    assert row.grade_reason is None
    conn.close()


def test_a_database_from_an_older_schema_says_what_to_delete(tmp_path):
    """The refusal is correct; a refusal the user cannot act on is not.

    There is no migration path, so an existing .whetstone/ from a build before
    the grade columns must error rather than silently mismatch. The message has
    to name the file, because the only fix available is deleting it and the
    user cannot be expected to guess that.
    """
    connect(tmp_path).close()
    with sqlite3.connect(tmp_path / "whetstone.db") as raw:
        raw.execute("PRAGMA user_version=1")
    with pytest.raises(SchemaVersionError) as excinfo:
        connect(tmp_path)
    message = str(excinfo.value)
    assert "whetstone.db" in message
    assert "delete" in message.lower()


def test_state_survives_reupsert(tmp_path):
    conn = connect(tmp_path)
    upsert(conn, _candidate(), "run-1", NOW)
    conn.execute("UPDATE findings SET state = 'rejected'")
    upsert(conn, _candidate(), "run-2", NOW)
    assert list_findings(conn)[0].state == "rejected"
    conn.close()


def test_list_findings_filters(tmp_path):
    conn = connect(tmp_path)
    upsert(conn, _candidate("a"), "run-1", NOW)
    upsert(conn, _candidate("b"), "run-1", NOW)
    conn.execute("UPDATE findings SET state = 'rejected' WHERE subject = 'a'")
    assert len(list_findings(conn, state="queued")) == 1
    assert count_by_state(conn) == {"queued": 1, "rejected": 1}
    conn.close()


def test_severity_ranks_before_alphabet(tmp_path):
    """'low' sorts before 'medium' alphabetically. Ranking must not.

    critical-vs-medium alone doesn't discriminate: "critical" < "medium"
    alphabetically too, so a naive `ORDER BY severity ASC` would pass that
    case by accident. low-vs-medium is where alphabetical and rank order
    actually disagree ("low" < "medium" alphabetically, but medium outranks
    low), so it has to be in the mix for this to be a real regression test.
    """
    conn = connect(tmp_path)
    for subject, severity in (
        ("a", Severity.medium),
        ("b", Severity.critical),
        ("c", Severity.low),
    ):
        candidate = Candidate(
            lens="hygiene",
            rule_id="R",
            subject=subject,
            title="t",
            detail="d",
            severity=severity,
            evidence=Evidence(EvidenceKind.metric, "s", {}),
        )
        upsert(conn, candidate, "run-1", NOW)
    assert [f.severity for f in list_findings(conn)] == ["critical", "medium", "low"]
    conn.close()


def test_evidence_round_trips(tmp_path):
    conn = connect(tmp_path)
    upsert(conn, _candidate(), "run-1", NOW)
    found = list_findings(conn)[0]
    assert found.evidence["data"]["package"] == "requests"
    conn.close()


def test_upsert_refreshes_wording_and_severity_but_not_state(tmp_path):
    """A re-scored or reworded finding updates in place; a rejection doesn't.

    `Candidate.dedupe_key` deliberately excludes title, detail, and severity
    so a reworded or re-scored candidate is still recognised as the same
    finding — that's only useful if the new wording and score actually reach
    the stored row on the next run. `state` is the one column this must
    never touch.
    """
    conn = connect(tmp_path)
    upsert(conn, _candidate(), "run-1", NOW)
    conn.execute("UPDATE findings SET state = 'rejected'")

    escalated = Candidate(
        lens="hygiene",
        rule_id="CVE-2026-1",
        subject="requests",
        title="requests has a CRITICAL known vulnerability",
        detail="Upgrade immediately — actively exploited.",
        severity=Severity.critical,
        evidence=Evidence(
            kind=EvidenceKind.metric,
            summary="pip-audit reported 1 advisory",
            data={"advisory": "CVE-2026-1", "package": "requests"},
        ),
    )
    upsert(conn, escalated, "run-2", NOW)

    found = list_findings(conn)[0]
    assert found.title == "requests has a CRITICAL known vulnerability"
    assert found.detail == "Upgrade immediately — actively exploited."
    assert found.severity == "critical"
    assert found.state == "rejected"
    conn.close()


def test_upsert_survives_a_concurrent_insert_race(tmp_path, monkeypatch):
    """The existence check and the insert are two statements, not one
    transaction: two callers can both miss the check and both attempt the
    insert. The loser must not raise — it has to recognise the row exists
    now and report `False`.

    Real thread interleaving isn't deterministic enough to pin this down
    reliably, so the miss is forced directly: insert the row for real, then
    patch the existence check to report it isn't there, and let the
    resulting INSERT hit the same UNIQUE-constraint failure a genuine race
    would produce.
    """
    import whetstone.store.findings as findings_module

    conn = connect(tmp_path)
    candidate = _candidate()
    upsert(conn, candidate, "run-1", NOW)
    conn.execute("UPDATE findings SET state = 'rejected'")

    monkeypatch.setattr(findings_module, "_existing_id", lambda conn, key: None)

    assert upsert(conn, candidate, "run-2", NOW) is False
    assert list_findings(conn)[0].state == "rejected"
    conn.close()


def test_connect_sets_a_busy_timeout(tmp_path):
    conn = connect(tmp_path)
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 30000
    conn.close()


def test_connect_refuses_a_mismatched_schema_version(tmp_path):
    from whetstone.errors import SchemaVersionError

    conn = connect(tmp_path)
    conn.execute("PRAGMA user_version=99")
    conn.close()

    with pytest.raises(SchemaVersionError):
        connect(tmp_path)


@pytest.mark.parametrize("field", ["lens", "rule_id", "title", "detail"])
def test_a_missing_required_field_raises_instead_of_reporting_seen(tmp_path, field):
    """A None in one field is a NOT NULL violation -- which is also an
    `sqlite3.IntegrityError`. A handler written for the dedupe-key race used to
    swallow it, report the finding as already seen, and drop it on the floor.

    `Candidate.__post_init__` now refuses this candidate at construction (issue
    #14), so the field is planted through `object.__setattr__` to reach the
    store's own handler regardless. The two layers are independent and the
    inner one keeps its own test: a future field that is NOT NULL in the schema
    but not yet covered by the lens contract would otherwise have no gate at
    all, which is how this defect arrived the first time.
    """
    conn = connect(tmp_path)
    candidate = _candidate()
    object.__setattr__(candidate, field, None)
    with pytest.raises(sqlite3.IntegrityError):
        upsert(conn, candidate, "run-1", NOW)
    assert count_by_state(conn) == {}
    assert list_findings(conn) == []
    conn.close()


def test_upsert_race_applies_the_refresh(tmp_path, monkeypatch):
    """The race path must do everything the normal update path does: report
    `False` AND land the new wording and severity on the stored row."""
    import whetstone.store.findings as findings_module

    conn = connect(tmp_path)
    upsert(conn, _candidate(), "run-1", NOW)
    monkeypatch.setattr(findings_module, "_existing_id", lambda conn, key: None)

    reworded = replace(
        _candidate(), title="reworded", detail="new detail", severity=Severity.critical
    )
    assert upsert(conn, reworded, "run-2", LATER) is False

    found = list_findings(conn)[0]
    assert found.title == "reworded"
    assert found.detail == "new detail"
    assert found.severity == "critical"
    assert found.last_seen_run == "run-2"
    assert found.updated_at == LATER
    conn.close()


def test_a_refresh_that_matches_no_row_raises(tmp_path, monkeypatch):
    """The backstop. If the existence check says a row is there and the UPDATE
    finds nothing, the candidate has been silently discarded -- exactly the
    outcome the narrowed handler above exists to prevent, arrived at by a
    different route."""
    import whetstone.store.findings as findings_module

    conn = connect(tmp_path)
    monkeypatch.setattr(findings_module, "_existing_id", lambda conn, key: "phantom")
    with pytest.raises(StoreError):
        upsert(conn, _candidate(), "run-1", NOW)
    assert list_findings(conn) == []
    conn.close()


# --- opening a brand-new database from several threads at once ----------------


def test_concurrent_first_opens_all_succeed_and_all_get_wal(tmp_path):
    """MEASURED FIRST, then fixed. Three threads opening a fresh database
    failed with `OperationalError: database is locked` in roughly half of
    twenty-five trials.

    Switching journal modes takes an EXCLUSIVE lock and SQLite returns
    SQLITE_BUSY for it immediately rather than calling the busy handler, so the
    connection's 30-second timeout -- which covers every ordinary write -- did
    nothing here.

    Not an exotic case: the control plane's first page load fires
    `/api/findings`, `/api/trust` and `/api/costs` in parallel, each opening
    its own connection. A fresh project met this on its first screen about half
    the time and got a 500 with no explanation on either side.

    FOUR WORKERS AND SEVERAL TRIALS, because one trial of one open reproduces
    nothing -- the race window is the first milliseconds of a database's life,
    so the test has to create a new database each time.
    """
    import concurrent.futures

    for trial in range(12):
        root = tmp_path / f"trial-{trial}" / "state"

        def _open(_index, root=root):
            conn = connect(root)
            try:
                return str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
            finally:
                conn.close()

        with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
            modes = list(pool.map(_open, range(4)))

        assert modes == ["wal"] * 4, (trial, modes)


def test_a_database_already_in_wal_is_not_switched_again(tmp_path, monkeypatch):
    """The read-before-write half, which is what makes the retry converge.

    `journal_mode` is a persistent property of the FILE, so every open after
    the first only has to read it back -- and reading takes no exclusive lock.
    Without this the common path would contend on every single open rather
    than only during a database's first milliseconds.
    """
    root = tmp_path / "state"
    first = connect(root)
    first.close()

    # A TRACE CALLBACK, because `sqlite3.Connection` is an immutable type and
    # its `execute` cannot be patched. The callback sees every statement the
    # connection actually runs, which is a stronger observation anyway.
    statements: list[str] = []
    real_connect = sqlite3.connect

    def _traced(*args, **kwargs):
        conn = real_connect(*args, **kwargs)
        conn.set_trace_callback(statements.append)
        return conn

    monkeypatch.setattr(db_module.sqlite3, "connect", _traced)
    second = connect(root)
    second.close()

    assert statements, "the trace callback saw nothing; it is not measuring"
    assert not any("journal_mode=WAL" in sql for sql in statements), (
        "an already-WAL database was asked to switch again"
    )


def test_a_non_lock_operational_error_is_not_retried():
    """`OperationalError` is not a synonym for "somebody else holds it".

    It also covers "file is not a database", "disk I/O error" and "attempt to
    write a readonly database" -- none of which another five seconds of
    retrying will fix, and every one of which would otherwise have been
    re-reported as a WAL contention problem, sending the reader to look for a
    second Whetstone process that does not exist.

    Asserted on the predicate the retry loop consults rather than by forcing a
    corrupt database into `connect`: the branch is one `if`, and a test that
    manufactures a torn file to reach it measures the manufacturing.
    """
    assert db_module._is_lock_error(sqlite3.OperationalError("database is locked"))
    assert db_module._is_lock_error(
        sqlite3.OperationalError("database table is locked")
    )
    for fatal in (
        "file is not a database",
        "disk I/O error",
        "attempt to write a readonly database",
        "no such table: findings",
    ):
        assert not db_module._is_lock_error(sqlite3.OperationalError(fatal)), fatal


class _FakeCursor:
    def __init__(self, row):
        self._row = row

    def fetchone(self):
        return self._row


class _NotADatabase:
    """A connection whose WAL switch fails for a reason retrying cannot fix."""

    def __init__(self, failure: str):
        self.failure = failure
        self.wal_attempts = 0

    def execute(self, sql: str, *_args):
        if "journal_mode=WAL" in sql:
            self.wal_attempts += 1
            raise sqlite3.OperationalError(self.failure)
        if "journal_mode" in sql:
            return _FakeCursor(("delete",))
        return _FakeCursor(None)


def test_a_fatal_open_error_propagates_instead_of_being_retried(tmp_path):
    """THE BRANCH, not just the predicate it consults.

    A mutation battery removed `if not _is_lock_error(exc): raise` and the
    predicate test above stayed green -- it asserted the function classifies
    correctly, never that the retry loop acts on the classification. So this
    drives `_enable_wal` with a connection that fails for a fatal reason and
    requires the error to come straight back out, ONCE.
    """
    conn = _NotADatabase("file is not a database")

    with pytest.raises(sqlite3.OperationalError) as caught:
        db_module._enable_wal(conn, tmp_path / "whetstone.db")

    assert "not a database" in str(caught.value)
    assert conn.wal_attempts == 1, (
        f"a fatal error was retried {conn.wal_attempts} times; it is not "
        "contention and no amount of waiting fixes it"
    )


def test_a_lock_error_IS_retried_so_the_test_above_is_not_vacuous(tmp_path):
    """The counterweight. Without it, an implementation that never retried
    anything would satisfy the assertion above."""
    conn = _NotADatabase("database is locked")

    with pytest.raises(StoreError):
        db_module._enable_wal(conn, tmp_path / "whetstone.db")

    assert conn.wal_attempts > 1, "a lock error must be retried, not given up on"
