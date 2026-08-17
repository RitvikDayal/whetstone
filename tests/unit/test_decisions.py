"""The decision ledger, and the acceptance rate computed from it.

The rate is the number the Trust screen turns into "is this tool trustworthy
here". Every classification below decides whether a real decision counts, and
getting one wrong does not fail loudly -- it produces a plausible number that
is wrong in a direction nobody can see.
"""

import pytest

from whetstone.grade import Grade
from whetstone.lenses.base import Candidate, Evidence, EvidenceKind, Severity
from whetstone.queue.decisions import acceptance_rate, decisions_for, record
from whetstone.queue.dispositions import Disposition, apply
from whetstone.store.db import connect
from whetstone.store.findings import list_findings, upsert

NOW = "2026-08-17T10:00:00+00:00"
LATER = "2026-08-18T10:00:00+00:00"


def _candidate(subject="orders.py:9", lens="code-defects") -> Candidate:
    return Candidate(
        lens=lens,
        rule_id="defect",
        subject=subject,
        title="t",
        detail="d",
        severity=Severity.high,
        evidence=Evidence(EvidenceKind.repro, "s", {}),
        grade=Grade.A,
        grade_reason="graded A: reproduced.",
    )


def _finding(conn, subject="orders.py:9", lens="code-defects") -> str:
    upsert(conn, _candidate(subject, lens), "run-1", NOW)
    return next(f.id for f in list_findings(conn) if f.subject == subject)


def _decide(conn, subject, disposition, lens="code-defects", **kwargs):
    fid = _finding(conn, subject, lens)
    kwargs.setdefault("now", NOW)
    return apply(conn, fid, disposition, **kwargs)


# --- the six classifications, one test each -----------------------------------
#
# Named rather than parametrised for the two that matter most, because the
# reason each is classified the way it is does not fit in a parameter.


def test_verify_is_an_acceptance(tmp_path):
    conn = connect(tmp_path)
    _decide(conn, "a.py:1", Disposition.verify)
    assert acceptance_rate(conn, "code-defects") == (1.0, 1)


def test_implement_is_an_acceptance(tmp_path):
    conn = connect(tmp_path)
    _decide(conn, "a.py:1", Disposition.implement)
    assert acceptance_rate(conn, "code-defects") == (1.0, 1)


def test_hand_off_is_an_acceptance(tmp_path):
    """The human agreed it was real and named someone. Counting it as a
    rejection makes the tool look worse than it is, which is the direction
    that gets a lens switched off."""
    conn = connect(tmp_path)
    _decide(conn, "a.py:1", Disposition.hand_off, assignee="ritvik")
    assert acceptance_rate(conn, "code-defects") == (1.0, 1)


def test_reject_is_a_rejection(tmp_path):
    conn = connect(tmp_path)
    _decide(conn, "a.py:1", Disposition.reject, reason="intended")
    assert acceptance_rate(conn, "code-defects") == (0.0, 1)


def test_defer_is_neither_and_is_excluded_from_both(tmp_path):
    """"Not now" is not a judgement about whether the finding is real. Counted
    as a rejection, a busy week reads as a quality collapse and demotes a lens
    that did nothing wrong."""
    conn = connect(tmp_path)
    _decide(conn, "a.py:1", Disposition.defer, wake="2026-09-01")
    assert acceptance_rate(conn, "code-defects") == (None, 0)


def test_needs_evidence_is_neither_and_is_excluded_from_both(tmp_path):
    """"Not yet" is a request, not a verdict."""
    conn = connect(tmp_path)
    _decide(conn, "a.py:1", Disposition.needs_evidence, reason="no repro")
    assert acceptance_rate(conn, "code-defects") == (None, 0)


# --- the rate never travels without its sample size ---------------------------


def test_no_decisions_gives_none_not_zero(tmp_path):
    """`0.0` reads as "everything was rejected". The two are opposite claims
    and a caller cannot tell them apart from a bare float."""
    conn = connect(tmp_path)
    _finding(conn)
    assert acceptance_rate(conn, "code-defects") == (None, 0)


def test_a_lens_that_has_never_run_gives_none_not_zero(tmp_path):
    conn = connect(tmp_path)
    assert acceptance_rate(conn, "rendered-ui") == (None, 0)


def test_the_sample_size_is_the_counted_decisions_not_every_row(tmp_path):
    """Three decisions, one of them a deferral: the rate is over two."""
    conn = connect(tmp_path)
    _decide(conn, "a.py:1", Disposition.verify)
    _decide(conn, "b.py:1", Disposition.reject, reason="no")
    _decide(conn, "c.py:1", Disposition.defer, wake="2026-09-01")
    assert acceptance_rate(conn, "code-defects") == (0.5, 2)


# --- per-lens isolation --------------------------------------------------------


def test_one_lens_does_not_move_anothers_rate(tmp_path):
    conn = connect(tmp_path)
    _decide(conn, "a.py:1", Disposition.verify, lens="code-defects")
    _decide(conn, "requests", Disposition.reject, lens="hygiene", reason="wontfix")

    assert acceptance_rate(conn, "code-defects") == (1.0, 1)
    assert acceptance_rate(conn, "hygiene") == (0.0, 1)


# --- a reversal is recorded as a reversal --------------------------------------


def test_accepting_then_rejecting_counts_both(tmp_path):
    """Deliberate, and the kind of thing a reviewer reads as a bug.

    The human accepted it and then changed their mind. A rate that keeps only
    the latest decision per finding is calibrating against a tidied story
    rather than the record -- and the reversal is exactly the signal that the
    lens produced something that looked right and was not.
    """
    conn = connect(tmp_path)
    fid = _finding(conn)
    apply(conn, fid, Disposition.verify, now=NOW)
    apply(conn, fid, Disposition.reject, reason="looked real, was not", now=LATER)

    assert acceptance_rate(conn, "code-defects") == (0.5, 2)


# --- reading the ledger ---------------------------------------------------------


def test_decisions_for_returns_them_newest_last(tmp_path):
    conn = connect(tmp_path)
    fid = _finding(conn)
    apply(conn, fid, Disposition.needs_evidence, reason="a", now=NOW)
    apply(conn, fid, Disposition.verify, now=LATER)

    rows = decisions_for(conn)
    assert [d.disposition for d in rows] == ["needs_evidence", "verify"]
    assert rows[0].decided_at == NOW


def test_decisions_for_filters_by_lens_and_by_finding(tmp_path):
    conn = connect(tmp_path)
    _decide(conn, "a.py:1", Disposition.verify, lens="code-defects")
    _decide(conn, "requests", Disposition.verify, lens="hygiene")

    assert len(decisions_for(conn)) == 2
    assert len(decisions_for(conn, lens="hygiene")) == 1
    one = decisions_for(conn, lens="hygiene")[0]
    assert len(decisions_for(conn, finding_id=one.finding_id)) == 1


def test_a_decision_carries_the_argument_that_was_required(tmp_path):
    conn = connect(tmp_path)
    _decide(conn, "a.py:1", Disposition.defer, wake="when 3.13 ships")
    (decision,) = decisions_for(conn)
    assert decision.wake == "when 3.13 ships"
    assert decision.reason is None
    assert decision.from_state == "queued"
    assert decision.to_state == "deferred"


# --- one writer -----------------------------------------------------------------


def test_record_is_the_only_writer_and_apply_goes_through_it(tmp_path, monkeypatch):
    """Two writers of one table means the second one written forgets a column.

    `upsert` and `_refresh` disagreeing about what a refresh updates already
    cost this project a run; that was two writers of one row.
    """
    import whetstone.queue.dispositions as dispositions_module

    calls = []
    real = dispositions_module.record

    def _spy(conn, **kwargs):
        calls.append(kwargs)
        return real(conn, **kwargs)

    monkeypatch.setattr(dispositions_module, "record", _spy)
    conn = connect(tmp_path)
    _decide(conn, "a.py:1", Disposition.verify)

    assert len(calls) == 1
    assert calls[0]["disposition"] == "verify"
    assert calls[0]["from_state"] == "queued"
    assert calls[0]["to_state"] == "verified"


def test_record_writes_every_non_null_column(tmp_path):
    """A recorder that cannot write every NOT NULL column is not the table's
    writer, it is one of two."""
    conn = connect(tmp_path)
    fid = _finding(conn)
    record(
        conn,
        finding_id=fid,
        lens="code-defects",
        disposition="verify",
        from_state="queued",
        to_state="verified",
        reason=None,
        wake=None,
        assignee=None,
        now=NOW,
    )
    (decision,) = decisions_for(conn)
    assert decision.finding_id == fid
    assert decision.lens == "code-defects"
    assert decision.decided_at == NOW


def test_two_decisions_do_not_share_an_id(tmp_path):
    conn = connect(tmp_path)
    fid = _finding(conn)
    apply(conn, fid, Disposition.needs_evidence, reason="a", now=NOW)
    apply(conn, fid, Disposition.verify, now=LATER)
    ids = {d.id for d in decisions_for(conn)}
    assert len(ids) == 2


@pytest.mark.parametrize("rate_for", ["code-defects", "hygiene"])
def test_the_rate_is_a_fraction_not_a_percentage(tmp_path, rate_for):
    """0.6 and 60 are both plausible and only one is what the thresholds in
    `autonomy.py` compare against."""
    conn = connect(tmp_path)
    for i in range(3):
        _decide(conn, f"a{i}.py:1", Disposition.verify, lens=rate_for)
    for i in range(2):
        _decide(conn, f"b{i}.py:1", Disposition.reject, lens=rate_for, reason="no")
    rate, n = acceptance_rate(conn, rate_for)
    assert n == 5
    assert 0.0 <= rate <= 1.0
    assert rate == pytest.approx(0.6)
