"""Earned autonomy: a number, and the sentence explaining it.

THIS ENFORCES NOTHING. No writer exists until M1b-2, so a level above 1 has
nothing to authorise. Shipping the computation first means the number is
visible and calibrating before it can act on anything -- and the tests below
pin that absence, so a later reader cannot mistake it for an oversight.
"""

from datetime import UTC, datetime, timedelta

import pytest

from whetstone.grade import Grade
from whetstone.lenses.base import Candidate, Evidence, EvidenceKind, Severity
from whetstone.queue.autonomy import (
    DEMOTION_RATE,
    PROBATION_LEVEL,
    PROMOTION_DECISIONS,
    PROMOTION_RATE,
    TRAILING_WINDOW,
    earned_level,
)
from whetstone.queue.dispositions import Disposition, apply
from whetstone.store.db import connect
from whetstone.store.findings import list_findings, upsert

NOW = "2026-08-17T10:00:00+00:00"
_BASE = datetime(2026, 8, 17, 10, 0, 0, tzinfo=UTC)


def _at(index: int) -> str:
    """An ISO timestamp that sorts by *index*. ISO-8601 is lexicographically
    ordered, which is what `decisions_for`'s ORDER BY relies on."""
    return (_BASE + timedelta(minutes=index)).isoformat()


@pytest.fixture
def store(tmp_path):
    conn = connect(tmp_path)
    yield conn
    conn.close()


def _decide(conn, index: int, disposition: Disposition, lens="code-defects", **kw):
    subject = f"f{index}.py:1"
    upsert(
        conn,
        Candidate(
            lens=lens,
            rule_id="r",
            subject=subject,
            title="t",
            detail="d",
            severity=Severity.high,
            evidence=Evidence(EvidenceKind.repro, "s", {}),
            grade=Grade.A,
            grade_reason="graded A: reproduced.",
        ),
        "run-1",
        NOW,
    )
    fid = next(f.id for f in list_findings(conn) if f.subject == subject)
    # Real datetimes, not f-string arithmetic on the minute field. The first
    # version built `10:{index:02d}:00`, so index 100 produced "10:100:00" --
    # which sorts BEFORE "10:20:00" lexicographically, and `decisions_for`
    # orders by `decided_at`. The trailing window was silently the OLDEST ten
    # decisions, and the demotion test failed for a reason that had nothing to
    # do with the code under test.
    apply(conn, fid, disposition, now=_at(index), **kw)


def _record(conn, accepted: int, rejected: int, lens="code-defects", start=0):
    i = start
    for _ in range(accepted):
        _decide(conn, i, Disposition.verify, lens=lens)
        i += 1
    for _ in range(rejected):
        _decide(conn, i, Disposition.reject, lens=lens, reason="not real")
        i += 1
    return i


# --- probation ----------------------------------------------------------------


def test_a_lens_with_no_record_is_on_probation(store):
    level, why = earned_level(store, "code-defects", 3, trust=None)
    assert level == PROBATION_LEVEL
    assert "no decisions" in why


def test_probation_never_exceeds_a_lower_ceiling(store):
    """A ceiling of 0 means report-only, and probation must not raise it."""
    level, _ = earned_level(store, "code-defects", 0, trust=None)
    assert level == 0


def test_a_perfect_record_below_the_decision_threshold_stays_on_probation(store):
    """Nine out of nine is not a track record, it is a small sample."""
    _record(store, accepted=PROMOTION_DECISIONS - 1, rejected=0)
    level, why = earned_level(store, "code-defects", 3, trust=None)
    assert level == PROBATION_LEVEL
    assert str(PROMOTION_DECISIONS - 1) in why


# --- promotion ----------------------------------------------------------------


def test_promotion_at_exactly_the_threshold(store):
    """6 of 10 is 60%, and the rule is >= 60%. One test either side of a
    boundary is the only thing that pins which comparison was written."""
    _record(store, accepted=6, rejected=4)
    level, why = earned_level(store, "code-defects", 3, trust=None)
    assert level == 3
    assert "10" in why


def test_one_below_the_rate_threshold_does_not_promote(store):
    _record(store, accepted=5, rejected=5)
    level, _ = earned_level(store, "code-defects", 3, trust=None)
    assert level == PROBATION_LEVEL


def test_promotion_never_exceeds_the_configured_ceiling(store):
    """The ceiling is the user's decision. A ceiling promotion can exceed is
    not a ceiling."""
    _record(store, accepted=10, rejected=0)
    level, _ = earned_level(store, "code-defects", 2, trust=None)
    assert level == 2


def test_a_flawless_lens_at_ceiling_zero_stays_at_zero(store):
    _record(store, accepted=20, rejected=0)
    level, _ = earned_level(store, "code-defects", 0, trust=None)
    assert level == 0


# --- demotion -----------------------------------------------------------------


def test_demotion_when_the_trailing_window_collapses(store):
    """Promotion reads the whole record; demotion reads the trailing window.

    The two are deliberately different: promotion needs a sustained record,
    demotion needs to react to a recent collapse. A single window for both
    makes one of the two wrong.
    """
    _record(store, accepted=20, rejected=0)
    nxt = _record(store, accepted=3, rejected=7, start=100)
    assert nxt  # the trailing 10 are now 30% accepted

    level, why = earned_level(store, "code-defects", 3, trust=None)
    assert level == PROBATION_LEVEL
    assert "trailing" in why.lower()


def test_a_demotion_reason_names_the_level_it_actually_returns(store):
    """With a ceiling of 0 the function returns 0 and the sentence claimed 1.

    A number and an explanation that disagree is worse than either alone: the
    explanation is the whole reason the number is trustworthy.
    """
    _record(store, accepted=0, rejected=10)
    level, why = earned_level(store, "code-defects", 0, trust=None)
    assert level == 0
    assert "level 0" in why


def test_exactly_at_the_demotion_rate_does_not_demote(store):
    """The rule is "below 40%", so 4 of the trailing 10 holds."""
    _record(store, accepted=20, rejected=0)
    _record(store, accepted=4, rejected=6, start=100)
    level, _ = earned_level(store, "code-defects", 3, trust=None)
    assert level == 3


def test_demotion_needs_a_full_window_before_it_fires(store):
    """Three rejections in a row on a young lens is not a collapse, it is
    three decisions."""
    _record(store, accepted=0, rejected=3)
    level, _ = earned_level(store, "code-defects", 3, trust=None)
    assert level == PROBATION_LEVEL  # on probation anyway, not demoted


# --- trust: assumed ------------------------------------------------------------


def test_trust_assumed_skips_probation(store):
    level, why = earned_level(store, "code-defects", 3, trust="assumed")
    assert level == 3
    assert "assumed" in why


def test_trust_assumed_does_not_skip_demotion(store):
    """The obvious wrong implementation reads `assumed` as "never demote".

    A lens the user asserted they trust, which then has 7 of its last 10
    rejected, is a lens the record disagrees with -- and the assertion was
    made before any of those decisions existed.
    """
    _record(store, accepted=3, rejected=7)
    level, why = earned_level(store, "code-defects", 3, trust="assumed")
    assert level == PROBATION_LEVEL
    assert "trailing" in why.lower()


# --- the sentence --------------------------------------------------------------


def test_the_reason_names_the_actual_counts_not_a_template(store):
    """"is this tool trustworthy here" becomes a number rather than a feeling,
    and a number without its reason is still a feeling."""
    _record(store, accepted=7, rejected=3)
    _, why = earned_level(store, "code-defects", 3, trust=None)
    assert "7" in why or "70" in why
    assert "10" in why


def test_the_reason_is_a_sentence_a_person_can_read(store):
    _, why = earned_level(store, "code-defects", 3, trust=None)
    assert why[0].isupper() or why.startswith("code-defects")
    assert why.endswith(".")
    assert len(why.split()) >= 6


def test_one_lens_record_does_not_promote_another(store):
    _record(store, accepted=10, rejected=0, lens="hygiene")
    level, _ = earned_level(store, "code-defects", 3, trust=None)
    assert level == PROBATION_LEVEL


# --- it enforces nothing --------------------------------------------------------


def test_earned_level_is_consulted_only_by_the_spine():
    """M1b-1's version of this asserted NOTHING read `earned_level`, because no
    writer existed and a number gating nothing was the deliberate state.

    M1b-2 gives it a consumer, so the assertion changes -- and changing it was
    that task's deliverable rather than a concession. What it now says is the
    property that survives: the SPINE routes and a LENS never does. A lens that
    could read its own earned level could act on it, and the design's
    load-bearing rule is that everything with consequences belongs to the spine.

    The specific list lives in `test_routing.py`, which owns the routing
    module; this asserts the half that matters here.
    """
    from pathlib import Path

    src = Path(__file__).resolve().parents[2] / "src" / "whetstone"
    files = sorted(src.rglob("*.py"))
    assert len(files) >= 5, f"the scan is not reaching src/: {files}"
    lens_readers = [
        p.relative_to(src).as_posix()
        for p in files
        if "earned_level" in p.read_text(encoding="utf-8")
        and p.relative_to(src).as_posix().startswith("lenses/")
    ]
    assert lens_readers == [], lens_readers


def test_the_thresholds_are_declared_as_recalibratable_constants():
    """They are hypotheses, not tuned constants -- the design says so. Named
    constants are what makes re-tuning one edit rather than a hunt."""
    assert 0 < PROMOTION_RATE <= 1
    assert 0 < DEMOTION_RATE < PROMOTION_RATE
    assert PROMOTION_DECISIONS >= TRAILING_WINDOW
