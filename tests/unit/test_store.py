import pytest

from whetstone.lenses.base import Candidate, Evidence, EvidenceKind, Severity
from whetstone.store.db import SCHEMA_VERSION, connect
from whetstone.store.findings import count_by_state, list_findings, upsert

NOW = "2026-08-10T10:00:00+00:00"


def _candidate(subject: str = "requests", rule_id: str = "CVE-2026-1") -> Candidate:
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
