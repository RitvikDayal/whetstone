"""M1b-1's definition of done: the queue, driven end to end.

GREEN UNIT TESTS DO NOT SATISFY THIS. Everything below goes through the real
CLI against a real git repository, and the rejection survives a real second
`whetstone run` rather than a SQL UPDATE standing in for one.

WHAT THIS DOES NOT COVER, STATED RATHER THAN IMPLIED. The grade A and grade D
rows here are REPLAYED from M1a's Task 10 measurement (2026-08-15), not
produced by a model in this test. Producing a fresh grade A needs the
`code-defects` lens, which needs the Claude CLI and a Linux-container Docker
daemon; without Docker the reproduction never executes and the finding is
capped at B by design. The replay is the stricter half of that trade -- those
verdicts were reached by a pipeline that had never seen this milestone's code
and cannot have been flattered by it -- but it is not a substitute for the one
real run, and the M1b-1 plan says so.

The lens driven live is `hygiene`, which makes no model call and needs no
container: a real detector, a real runner, a real store, a real CLI.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from whetstone.cli import app
from whetstone.grade import Grade
from whetstone.lenses.base import Candidate, Evidence, EvidenceKind, Severity
from whetstone.queue.autonomy import earned_level
from whetstone.queue.decisions import acceptance_rate
from whetstone.store.findings import list_findings, upsert

runner = CliRunner()

_FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "m1b1"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A real git repository whose coverage is genuinely below a floor."""
    _git(tmp_path, "init", "-b", "main")
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    (tmp_path / "pyproject.toml").write_text(
        "[project]\nname = 'demo'\nversion = '0.1.0'\n", encoding="utf-8"
    )
    (tmp_path / "coverage.xml").write_text(
        '<?xml version="1.0"?><coverage line-rate="0.30"/>', encoding="utf-8"
    )
    (tmp_path / "whetstone.yaml").write_text(
        "version: 1\n"
        "project:\n  name: demo\n"
        "state_dir: .whetstone-state\n"
        "lenses:\n"
        "  hygiene:\n"
        "    enabled: true\n"
        "    options:\n"
        "      coverage_floor: 80\n",
        encoding="utf-8",
    )
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "init", "--no-gpg-sign")
    return tmp_path


@pytest.fixture(autouse=True)
def _wide(monkeypatch):
    from whetstone.cli import console

    monkeypatch.setattr(console, "_width", 300)


def _invoke(*args: str):
    return runner.invoke(app, list(args))


def _store(project: Path):
    from whetstone.config.loader import find_config, load_config
    from whetstone.paths import state_root
    from whetstone.store.db import connect

    cfg = load_config(find_config(project))
    override = cfg.state_dir.get_secret_value() if cfg.state_dir is not None else None
    return connect(state_root(project, override))


def _one_finding_id(project: Path) -> str:
    conn = _store(project)
    try:
        rows = list_findings(conn, state=None)
        assert rows, "the fixture produced no finding -- it has regressed"
        return rows[0].id
    finally:
        conn.close()


def _run(project: Path):
    result = _invoke("run", "--path", str(project), "--full")
    assert result.exit_code == 0, result.stdout
    return result


# --- the definition of done ----------------------------------------------------


def test_a_rejection_survives_a_real_second_run(project):
    """The invariant the whole milestone rests on, through the real commands.

    Not a SQL UPDATE standing in for a decision: `whetstone decide` does it,
    and `whetstone run` really runs again afterwards.
    """
    _run(project)
    fid = _one_finding_id(project)

    decided = _invoke(
        "decide", fid[:8], "reject",
        "--reason", "coverage is tracked elsewhere",
        "--path", str(project), "--yes",
    )
    assert decided.exit_code == 0, decided.stdout

    _run(project)

    listed = _invoke("findings", "--path", str(project), "--state", "rejected")
    assert listed.exit_code == 0, listed.stdout
    assert "coverage" in listed.stdout.lower()

    queued = _invoke("findings", "--path", str(project), "--state", "queued")
    assert "No findings in state 'queued'." in queued.stdout, (
        "the rejected finding came back to the queue on the second run"
    )


def test_defer_and_hand_off_stay_open_across_a_real_rerun(project):
    """`handed_off` is a tracked open state. The founding failure of this
    project was "assigned to a human" becoming the same black hole as "never
    found"."""
    _run(project)
    fid = _one_finding_id(project)

    handed = _invoke(
        "decide", fid[:8], "hand_off", "--assignee", "ritvik",
        "--path", str(project), "--yes",
    )
    assert handed.exit_code == 0, handed.stdout

    _run(project)

    listed = _invoke("findings", "--path", str(project), "--state", "handed_off")
    assert listed.exit_code == 0, listed.stdout
    assert "coverage" in listed.stdout.lower(), (
        "a handed-off finding vanished from the queue instead of ageing in it"
    )


def test_defer_requires_its_wake_condition_through_the_cli(project):
    _run(project)
    fid = _one_finding_id(project)

    refused = _invoke("decide", fid[:8], "defer", "--path", str(project), "--yes")
    assert refused.exit_code != 0
    assert "wake" in refused.stdout
    assert "Traceback" not in refused.stdout

    accepted = _invoke(
        "decide", fid[:8], "defer", "--wake", "when coverage is wired up",
        "--path", str(project), "--yes",
    )
    assert accepted.exit_code == 0, accepted.stdout


def test_the_rate_and_the_level_travel_with_their_counts(project):
    """After a real decision, both numbers exist and both carry their sample."""
    _run(project)
    fid = _one_finding_id(project)
    _invoke(
        "decide", fid[:8], "verify", "--path", str(project), "--yes"
    )

    conn = _store(project)
    try:
        rate, sample = acceptance_rate(conn, "hygiene")
        assert rate == 1.0
        assert sample == 1

        level, why = earned_level(conn, "hygiene", 3, trust=None)
        assert level == 1, "one decision is not a track record"
        assert "1 decision" in why and "10" in why
    finally:
        conn.close()


# --- the graded pair, replayed from M1a's measurement --------------------------


def _replay(conn, path: Path) -> str:
    """Load a finding recorded by M1a into a store built by M1b-1.

    The recorded rows predate the grade columns -- M1a stored the grade inside
    `evidence_json` and nothing read it, which is the defect Task 1 exists to
    fix. Pulling it out here is the translation that makes the point: the same
    verdict, reached by a pipeline that never saw this code, now lands in a
    column the CLI reads.
    """
    (row,) = json.loads(path.read_text(encoding="utf-8"))
    evidence = json.loads(row["evidence_json"])
    grade = Grade(evidence["data"]["grade"])
    upsert(
        conn,
        Candidate(
            lens=row["lens"],
            rule_id=row["rule_id"],
            subject=row["subject"],
            title=row["title"],
            detail=row["detail"],
            severity=Severity(row["severity"]),
            evidence=Evidence(
                kind=EvidenceKind(evidence["kind"]),
                summary=evidence["summary"],
                data=evidence["data"],
            ),
            grade=grade,
            grade_reason=evidence["data"]["grade_reason"],
        ),
        "replay",
        "2026-08-15T00:00:00+00:00",
    )
    return grade


def test_the_recorded_verdicts_are_a_grade_a_and_a_grade_d(project):
    """The fixtures are the pair this milestone needs, and this asserts it.

    A replay whose inputs are not checked is a test of the replay.
    """
    conn = _store(project)
    try:
        assert _replay(conn, _FIXTURES / "buggy-findings.json") is Grade.A
        assert _replay(conn, _FIXTURES / "clean-findings.json") is Grade.D
    finally:
        conn.close()


def test_a_killed_finding_is_distinguishable_from_a_survivor_at_a_glance(project):
    """M1a's measurement ended here: the falsifier killed a candidate on the
    clean repository and `whetstone findings` printed it exactly like the
    grade A from the buggy one."""
    conn = _store(project)
    try:
        _replay(conn, _FIXTURES / "buggy-findings.json")
        _replay(conn, _FIXTURES / "clean-findings.json")
    finally:
        conn.close()

    listed = _invoke("findings", "--path", str(project))
    assert listed.exit_code == 0, listed.stdout

    lines = listed.stdout.splitlines()
    survivor = next(ln for ln in lines if "orders.py:9" in ln)
    killed = next(ln for ln in lines if "orders.py:16" in ln)

    assert "A" in survivor
    assert "killed" in killed.lower()
    assert "killed" not in survivor.lower()

    # NO ORDERING ASSERTION HERE, deliberately. These are the real recorded
    # severities -- the grade A is `medium` and the grade D is `low` -- so
    # severity-first ordering puts them in the same order grade-first does, and
    # an assertion on their positions passes either way. Proven: reverting
    # `list_findings` to severity-first leaves this test green.
    #
    # `test_findings_are_ordered_by_grade_not_by_the_models_severity_claim` in
    # tests/unit/test_cli.py is the one that discriminates; it seeds the killed
    # finding `critical` and the survivor `low` on purpose.


def test_the_killed_finding_can_be_filtered_out(project):
    conn = _store(project)
    try:
        _replay(conn, _FIXTURES / "buggy-findings.json")
        _replay(conn, _FIXTURES / "clean-findings.json")
    finally:
        conn.close()

    only_a = _invoke("findings", "--path", str(project), "--grade", "A")
    assert only_a.exit_code == 0, only_a.stdout
    assert "orders.py:9" in only_a.stdout
    assert "orders.py:16" not in only_a.stdout


def test_rejecting_a_replayed_finding_survives_a_real_run(project):
    """The two halves joined: a verdict M1a produced, rejected through the CLI
    this milestone added, surviving a run the runner really performed."""
    conn = _store(project)
    try:
        _replay(conn, _FIXTURES / "clean-findings.json")
        fid = next(f.id for f in list_findings(conn, state=None) if "orders" in f.subject)
    finally:
        conn.close()

    decided = _invoke(
        "decide", fid[:8], "reject", "--reason", "the falsifier already killed it",
        "--path", str(project), "--yes",
    )
    assert decided.exit_code == 0, decided.stdout

    _run(project)

    listed = _invoke("findings", "--path", str(project), "--state", "rejected")
    assert "orders.py:16" in listed.stdout
