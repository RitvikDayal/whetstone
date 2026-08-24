"""Every surface renders one truth. Measured, not asserted in a docstring.

WHY THIS TEST IS SHAPED THE WAY IT IS. The obvious version -- route the CLI and
the read model through one function, then assert they agree -- is vacuous. It
reduces to asserting that a list comprehension preserves order, and it stays
green against a read model that is wrong about every field and against a
`list_findings` whose own `ORDER BY` is broken, because both sides read the
same broken order.

So the expected order is computed HERE, independently, from the rule
`store/findings.py` documents in prose:

    grade rank: A=0, B=1, (absent)=2, C=3, D=4
    then severity rank: critical=0, high=1, medium=2, everything else=3
    then subject ascending, then id ascending

Nothing in this file calls the production ordering to find out what the
production ordering is. Edit the SQL `CASE` and this goes red; that is the
whole point.

AND THE RENDER LAYER IS CHECKED, not just the query. All three failures this
milestone exists to prevent happened at the render layer, not in the query:
the falsifier's verdict was computed and never reached the list; a grade D
rendered identically to a grade A; `get_last_run` selected the status column
and dropped it. A test that stops at the read model is one layer below every
one of them, so the CLI's actual terminal output is parsed back and compared.
"""

from __future__ import annotations

import contextlib
import re
from pathlib import Path

import pytest
from typer.testing import CliRunner

from whetstone.cli import app
from whetstone.grade import Grade
from whetstone.lenses.base import Candidate, Evidence, EvidenceKind
from whetstone.readmodel import ID_PREFIX, findings_view, run_view
from whetstone.severity import Severity
from whetstone.store.db import connect
from whetstone.store.findings import list_findings, upsert

runner = CliRunner()

# The documented rule, restated as data. This is a DELIBERATE second copy of
# what the SQL encodes -- the two are supposed to be compared, and a single
# copy imported from the module under test would compare it with itself.
_GRADE_RANK = {"A": 0, "B": 1, None: 2, "C": 3, "D": 4}
_SEVERITY_RANK = {"critical": 0, "high": 1, "medium": 2}


def _expected_order(rows) -> list[str]:
    """Ids in the order `store/findings.py`'s prose says they should come."""
    return [
        row.id
        for row in sorted(
            rows,
            key=lambda r: (
                _GRADE_RANK[r.grade],
                _SEVERITY_RANK.get(r.severity, 3),
                r.subject,
                r.id,
            ),
        )
    ]


def _candidate(
    *, subject: str, title: str, severity: str, grade: str | None, key: str
) -> Candidate:
    return Candidate(
        lens="code-defects",
        # The dedupe key is derived from (lens, rule_id, subject), so the
        # three rows sharing a subject need distinct rule ids to be three rows.
        rule_id=key,
        subject=subject,
        title=title,
        detail="detail for " + title,
        severity=Severity(severity),
        evidence=Evidence(kind=EvidenceKind.metric, summary="seeded", data={}),
        grade=None if grade is None else Grade(grade),
        grade_reason=None if grade is None else f"graded {grade}",
    )


# Deliberately includes TIES that are total on every column but the id:
# same grade, same severity, same subject. Those are the rows that made the
# ordering non-deterministic before `id ASC` was appended, and they are the
# rows a flaky parity test would flake on.
_SEEDS = [
    ("app/pay.py", "divide by zero", "critical", "A", "k1"),
    ("app/pay.py", "same file same grade same severity", "critical", "A", "k2"),
    ("app/pay.py", "third of the identical trio", "critical", "A", "k3"),
    ("app/auth.py", "token never expires", "high", "A", "k4"),
    ("app/ui.py", "overlap", "medium", "B", "k5"),
    ("app/dep.py", "CVE-2024-0001", "high", None, "k6"),
    ("app/old.py", "probably fine", "low", "C", "k7"),
    ("app/no.py", "refuted by the falsifier", "critical", "D", "k8"),
    ("app/no2.py", "also refuted", "high", "D", "k9"),
]


@pytest.fixture
def seeded(tmp_path: Path):
    """A project with a config, a store, and one recorded run."""
    (tmp_path / "whetstone.yaml").write_text(
        "version: 1\nproject:\n  name: parity\nstate_dir: .state\n",
        encoding="utf-8",
    )
    root = tmp_path / ".state"
    conn = connect(root)
    conn.execute(
        "INSERT INTO runs (id, tier, scope_mode, file_count, started_at, "
        "status, skipped_json) VALUES (?, 'deep', 'full', 9, "
        "'2026-08-24T10:00:00+00:00', 'complete', ?)",
        ("run-0000000001", '["code-defects: one angle was not run"]'),
    )
    for subject, title, severity, grade, key in _SEEDS:
        upsert(
            conn,
            _candidate(
                subject=subject, title=title, severity=severity, grade=grade, key=key
            ),
            "run-0000000001",
            "2026-08-24T10:00:00+00:00",
        )
    yield tmp_path, conn
    conn.close()


def _cli(tmp_path: Path, *args: str) -> str:
    result = runner.invoke(app, [*args, "--path", str(tmp_path)])
    assert result.exit_code == 0, result.output
    return result.output


def _ids_in(output: str) -> list[str]:
    """The short ids the CLI printed, in the order it printed them.

    Anchored to a Rich table cell rather than searched loosely: a bare
    8-hex-character search also matches a hex digit run inside a title, and a
    parity test that silently picks up extra "ids" agrees with anything.

    The delimiter is Rich's box-drawing vertical (U+2502), written as an escape
    so this file stays ASCII. Asserting against what `rich.Table` ACTUALLY
    emits is the point: the first version of this parser expected "|", matched
    nothing, and would have reported perfect agreement between a populated API
    and an empty terminal.
    """
    return re.findall("^│\\s*([0-9a-f]{8})\\s", output, flags=re.MULTILINE)


def test_the_store_order_matches_the_rule_it_documents(seeded):
    """The independent half. Nothing here asks the code what its order is."""
    _, conn = seeded
    rows = list_findings(conn, state=None)
    assert [r.id for r in rows] == _expected_order(rows)


def test_the_read_model_preserves_the_store_order(seeded):
    _, conn = seeded
    rows = list_findings(conn, state=None)
    view = findings_view(conn, state=None)
    assert [v["id"] for v in view] == _expected_order(rows)


def test_the_cli_prints_the_same_findings_in_the_same_order(seeded):
    """The render layer, which is where all three historical failures were."""
    tmp_path, conn = seeded
    view = findings_view(conn, state="queued")
    printed = _ids_in(_cli(tmp_path, "findings"))
    assert printed == [v["short_id"] for v in view]
    assert len(printed) == len(_SEEDS)


def test_a_killed_finding_is_killed_on_every_surface(seeded):
    """The specific defect: a grade D that renders like a grade A."""
    tmp_path, conn = seeded
    view = findings_view(conn, state="queued")
    killed = [v for v in view if v["killed"]]
    assert killed, "the fixture must contain a killed finding or this proves nothing"

    output = _cli(tmp_path, "findings")
    assert output.count("killed") >= len(killed)
    for finding in killed:
        assert finding["grade"] == "D"
        assert finding["graded"] is True


def test_an_ungraded_finding_is_never_reported_as_killed(seeded):
    """Absent and D are different facts and every surface has to keep them so."""
    _, conn = seeded
    ungraded = [v for v in findings_view(conn, state=None) if not v["graded"]]
    assert ungraded, "the fixture must contain an ungraded finding"
    for finding in ungraded:
        assert finding["grade"] is None
        assert finding["killed"] is False


def test_the_cli_default_and_the_read_model_default_are_not_the_same_query(seeded):
    """A trap, pinned rather than left to be discovered by a second surface.

    `whetstone findings` defaults `--state` to `queued`; `findings_view`
    defaults `state` to None, which means DO NOT FILTER. A control plane that
    calls the read model with no arguments and calls it "the same list the CLI
    shows" is showing a different list. Both are correct; they are not
    interchangeable, and this is the assertion that says so out loud.
    """
    _, conn = seeded
    conn.execute("UPDATE findings SET state = 'rejected' WHERE rule_id = 'k7'")

    unfiltered = findings_view(conn, state=None)
    queued = findings_view(conn, state="queued")
    assert len(unfiltered) == len(_SEEDS)
    assert len(queued) == len(_SEEDS) - 1


def test_the_run_view_carries_the_status_the_cli_warns_about(seeded):
    """`get_last_run` once dropped this column and a broken run read as clean."""
    _, conn = seeded
    conn.execute("UPDATE runs SET status = 'failed'")

    view = run_view(conn)
    assert view is not None
    assert view["status"] == "failed"
    assert view["finished"] is False
    assert view["skips"] == ["code-defects: one angle was not run"]
    assert view["run_id"] == "run-0000000001"


def test_no_run_at_all_is_distinct_from_a_run_that_found_nothing(tmp_path):
    """None means nothing was checked. It must not render as "all clear"."""
    with contextlib.closing(connect(tmp_path / ".state")) as conn:
        assert run_view(conn) is None


def test_short_ids_are_long_enough_to_be_unique_in_the_fixture(seeded):
    """The prefix the CLI prints and `decide` accepts has to disambiguate."""
    _, conn = seeded
    shorts = [v["short_id"] for v in findings_view(conn, state=None)]
    assert len(set(shorts)) == len(shorts)
    assert all(len(s) == ID_PREFIX for s in shorts)
