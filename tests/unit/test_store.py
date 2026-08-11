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


def test_connect_is_idempotent(tmp_path):
    connect(tmp_path).close()
    conn = connect(tmp_path)
    assert conn.execute("SELECT COUNT(*) FROM findings").fetchone()[0] == 0


def test_upsert_reports_new_then_seen(tmp_path):
    conn = connect(tmp_path)
    assert upsert(conn, _candidate(), "run-1", NOW) is True
    assert upsert(conn, _candidate(), "run-2", NOW) is False
    rows = list_findings(conn)
    assert len(rows) == 1
    assert rows[0].first_seen_run == "run-1"
    assert rows[0].last_seen_run == "run-2"


def test_distinct_subjects_are_distinct_findings(tmp_path):
    conn = connect(tmp_path)
    upsert(conn, _candidate("requests"), "run-1", NOW)
    upsert(conn, _candidate("urllib3"), "run-1", NOW)
    assert len(list_findings(conn)) == 2


def test_state_survives_reupsert(tmp_path):
    conn = connect(tmp_path)
    upsert(conn, _candidate(), "run-1", NOW)
    conn.execute("UPDATE findings SET state = 'rejected'")
    upsert(conn, _candidate(), "run-2", NOW)
    assert list_findings(conn)[0].state == "rejected"


def test_list_findings_filters(tmp_path):
    conn = connect(tmp_path)
    upsert(conn, _candidate("a"), "run-1", NOW)
    upsert(conn, _candidate("b"), "run-1", NOW)
    conn.execute("UPDATE findings SET state = 'rejected' WHERE subject = 'a'")
    assert len(list_findings(conn, state="queued")) == 1
    assert count_by_state(conn) == {"queued": 1, "rejected": 1}


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


def test_evidence_round_trips(tmp_path):
    conn = connect(tmp_path)
    upsert(conn, _candidate(), "run-1", NOW)
    found = list_findings(conn)[0]
    assert found.evidence["data"]["package"] == "requests"
