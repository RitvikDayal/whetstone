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

# One data row of the table `whetstone findings` prints, capturing the short id
# and keeping the whole line -- so a test can ask what a SPECIFIC row said
# rather than what the document contained somewhere.
#
# `│` is Rich's box-drawing vertical, written as an escape so this file
# stays ASCII. Defined ONCE: two copies of this pattern drifted within an hour,
# one of them into a `\s` that Python read as an invalid escape and warned
# about rather than matching.
_ROW = re.compile("^\u2502" + r"\s*([0-9a-f]{8})\s.*$", re.MULTILINE)


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


# Rich sizes a table to the console, and `cli.console` is built at IMPORT time
# from whatever terminal (or absence of one) the suite happens to run under. At
# the 80-column default a long title is wrapped or elided, so an assertion about
# a title would fail on a narrow terminal and pass on a wide one -- a test whose
# verdict depends on the window it ran in. Pinned wide, once, for every
# invocation below.
_CONSOLE_WIDTH = 240


@pytest.fixture(autouse=True)
def _wide_console(monkeypatch):
    import whetstone.cli as cli_module

    monkeypatch.setattr(cli_module.console, "_width", _CONSOLE_WIDTH, raising=False)


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
    return [match.group(1) for match in _ROW.finditer(output)]


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

    # BOUND TO THE ROW, not counted in the document. `output.count("killed")`
    # also matches the footnote under the table, which says the word once per
    # listing -- so with one killed finding the count assertion passed against
    # a table whose grade column said nothing at all. The word has to be on the
    # same line as the id it describes.
    rows = {match.group(1): match.group(0) for match in _ROW.finditer(output)}
    assert len(rows) == len(_SEEDS)
    for finding in killed:
        assert finding["grade"] == "D"
        assert finding["graded"] is True
        assert "killed" in rows[finding["short_id"]], rows[finding["short_id"]]

    # ...and NOT on the rows that were not killed.
    for finding in view:
        if not finding["killed"]:
            assert "killed" not in rows[finding["short_id"]]


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


def test_a_malformed_stored_severity_cannot_break_or_style_the_listing(seeded):
    """The columns `Candidate` validates and the SCHEMA does not.

    `severity` and `grade` are plain TEXT in `store/db.py` with no CHECK
    constraint, so validation lives only on the write path a lens takes. A row
    written by a different build, hand-edited, or restored from a torn file can
    hold anything -- and Rich reads a bracket as markup. `[/x]` raises
    MarkupError and destroys the whole listing at the moment it prints;
    `[red]` is silently swallowed and the cell renders blank. `run`'s skip
    lines already shipped this defect once.
    """
    tmp_path, conn = seeded
    conn.execute(
        "UPDATE findings SET severity = ?, grade = ? WHERE rule_id = 'k1'",
        ("[/checkout @ 1280x800]", "[red]bogus[/red]"),
    )

    output = _cli(tmp_path, "findings")

    # It printed at all -- the MarkupError half.
    assert "divide by zero" in output
    assert len(_ids_in(output)) == len(_SEEDS)

    # And the markup is VISIBLE rather than applied -- the styling half,
    # asserted as literal text. The previous form was
    # `"[/checkout" in output or "checkout" in output`, and the second arm made
    # it nearly vacuous: "checkout" appears whether the brackets were escaped
    # or silently swallowed by Rich, which are the two outcomes being told
    # apart.
    assert "[/checkout @ 1280x800]" in output
    assert "[red]bogus[/red]" in output


# --- the cost view, which must not read damage as zero -----------------------


def _cost_record(state_root: Path, name: str, payload: dict) -> None:
    import json

    directory = state_root / "costs"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text(json.dumps(payload), encoding="utf-8")


def test_a_malformed_cost_field_is_reported_rather_than_read_as_zero(tmp_path):
    """The signature failure, in the one view whose job is showing spend.

    Nothing revalidates these files between runs, so a truncated write or a
    hand edit can leave `"spent_usd": null`. Coercing that to 0.0 turns real
    money into $0.00 on the screen built to show it -- the same silent
    under-count `Budget.spend` refuses to make one layer down, where an
    unmeasured call is counted as unmeasured rather than as free.
    """
    from whetstone.readmodel import cost_view

    _cost_record(
        tmp_path,
        "run-1.code-defects.json",
        {"run_id": "run-1", "lens": "code-defects", "spent_usd": None, "calls": 3,
         "unmeasured_calls": 0, "stages": []},
    )
    _cost_record(
        tmp_path,
        "run-2.code-defects.json",
        {"run_id": "run-2", "lens": "code-defects", "spent_usd": 0.25, "calls": 1,
         "unmeasured_calls": 0, "stages": []},
    )

    view = cost_view(tmp_path)

    assert view["total_usd"] == pytest.approx(0.25)
    assert any("spent_usd" in line and "run-1" in line for line in view["unreadable"])
    assert any("short by an unknown amount" in line for line in view["unreadable"])


def test_a_record_that_is_not_json_is_named(tmp_path):
    from whetstone.readmodel import cost_view

    (tmp_path / "costs").mkdir(parents=True)
    (tmp_path / "costs" / "run-1.code-defects.json").write_text("{ truncated",
                                                                encoding="utf-8")

    view = cost_view(tmp_path)

    assert view["records"] == []
    assert len(view["unreadable"]) == 1
    assert "run-1.code-defects.json" in view["unreadable"][0]


def test_a_clean_cost_directory_reports_nothing_unreadable(tmp_path):
    """The counterweight: a warning that always fires carries no information."""
    from whetstone.readmodel import cost_view

    _cost_record(
        tmp_path,
        "run-1.code-defects.json",
        {"run_id": "run-1", "lens": "code-defects", "spent_usd": 1.5, "calls": 4,
         "unmeasured_calls": 1, "stages": []},
    )

    view = cost_view(tmp_path)

    assert view["unreadable"] == []
    assert view["total_usd"] == pytest.approx(1.5)
    assert view["unmeasured_calls"] == 1
    assert view["lenses_with_records"] == ["code-defects"]


def test_no_cost_directory_at_all_is_empty_not_an_error(tmp_path):
    from whetstone.readmodel import cost_view

    view = cost_view(tmp_path)

    assert view["records"] == []
    assert view["total_usd"] == 0.0
    assert view["unreadable"] == []
