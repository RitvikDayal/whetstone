"""Routing: where autonomy stops being a number and becomes a gate.

M1b-1 shipped `earned_level` enforcing nothing, with a test asserting nothing
outside `queue/` read it. This is the module that reads it, and the tests below
are weighted towards the ways something could be published that a lens had not
earned -- plus the one case where the ceiling is deliberately bypassed, because
an explicit human decision is not autonomy.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from whetstone.grade import Grade
from whetstone.lenses.base import Candidate, Evidence, EvidenceKind, Severity
from whetstone.queue.dispositions import Disposition, apply
from whetstone.queue.routing import (
    Action,
    action_for,
    as_draft,
    may_publish_a_pull_request,
)
from whetstone.store.db import connect
from whetstone.store.findings import list_findings, upsert

NOW = "2026-08-17T10:00:00+00:00"


@pytest.fixture
def store(tmp_path):
    conn = connect(tmp_path)
    yield conn
    conn.close()


def _record(conn, accepted: int, rejected: int, lens="code-defects", start=0):
    """Decisions with STRICTLY INCREASING timestamps, continued across calls.

    `start` is not optional decoration. The first version restarted the clock
    at zero on every call, so a second batch interleaved with the first and the
    "trailing" window `autonomy` reads was not trailing at all -- the demotion
    test then failed for a reason that had nothing to do with routing. This is
    the third time a timestamp fixture on this project has produced that exact
    illusion.
    """
    from datetime import UTC, datetime, timedelta

    base = datetime(2026, 8, 17, 10, 0, tzinfo=UTC)
    index = start
    for disposition, count, kw in (
        (Disposition.verify, accepted, {}),
        (Disposition.reject, rejected, {"reason": "no"}),
    ):
        for _ in range(count):
            subject = f"f{index}.py:1"
            upsert(
                conn,
                Candidate(
                    lens=lens, rule_id="r", subject=subject, title="t", detail="d",
                    severity=Severity.high,
                    evidence=Evidence(EvidenceKind.repro, "s", {}),
                    grade=Grade.A, grade_reason="graded A: reproduced.",
                ),
                "run-1", NOW,
            )
            fid = next(f.id for f in list_findings(conn) if f.subject == subject)
            apply(conn, fid, disposition,
                  now=(base + timedelta(minutes=index)).isoformat(), **kw)
            index += 1
    return index


# --- there is no level 4 -----------------------------------------------------------


def test_there_are_exactly_four_actions_and_none_of_them_merges():
    """The invariant, asserted against the enum rather than described.

    Adding a fifth member -- or a `merge` -- fails here, and `test_invariants`
    fails separately on the command name anywhere under src/.
    """
    assert {a.value for a in Action} == {"report", "propose", "draft_pr", "open_pr"}
    assert not any("merge" in a.value or "deploy" in a.value for a in Action)


# --- a lens gets what it earned, bounded by what was configured ---------------------


def test_a_lens_with_no_record_only_reports_or_proposes(store):
    action, why = action_for(store, "code-defects", configured_ceiling=3, trust=None)
    assert action is Action.propose
    assert may_publish_a_pull_request(action) is False
    assert "probation" in why


def test_a_lens_that_earned_it_opens_a_ready_pull_request(store):
    _record(store, accepted=10, rejected=0)
    action, _ = action_for(store, "code-defects", configured_ceiling=3, trust=None)
    assert action is Action.open_pr
    assert as_draft(action) is False


def test_a_ceiling_of_two_caps_a_flawless_lens_at_a_draft(store):
    """The configured ceiling is the user's decision and promotion cannot
    exceed it."""
    _record(store, accepted=20, rejected=0)
    action, _ = action_for(store, "code-defects", configured_ceiling=2, trust=None)
    assert action is Action.draft_pr
    assert as_draft(action) is True


def test_a_ceiling_of_zero_publishes_nothing_however_good_the_record(store):
    _record(store, accepted=20, rejected=0)
    action, _ = action_for(store, "code-defects", configured_ceiling=0, trust=None)
    assert action is Action.report
    assert may_publish_a_pull_request(action) is False


def test_a_collapsed_trailing_record_stops_publishing(store):
    """Demotion has consequences here, which is the point of routing existing."""
    used = _record(store, accepted=20, rejected=0)
    _record(store, accepted=2, rejected=8, start=used)
    action, why = action_for(store, "code-defects", configured_ceiling=3, trust=None)
    assert may_publish_a_pull_request(action) is False
    assert "trailing" in why.lower()


def test_a_ceiling_above_three_is_not_permission_for_a_level_that_does_not_exist(store):
    """A config saying `autonomy: 7` is a typo. There is no level 4."""
    _record(store, accepted=20, rejected=0)
    action, _ = action_for(store, "code-defects", configured_ceiling=7, trust=None)
    assert action is Action.open_pr


# --- an explicit human decision is not autonomy --------------------------------------


def test_a_human_authorised_finding_bypasses_the_ceiling(store):
    """The design's rule: autonomy governs only what happens WITHOUT a human.

    A `product-ux` finding hard-capped at 1 can still be built when a person
    hands it back, and it then passes the same verification a crash bug does.
    """
    action, why = action_for(
        store, "product-ux", configured_ceiling=1, trust=None, human_authorised=True
    )
    assert action is Action.draft_pr
    assert "explicit" in why


def test_a_human_authorised_finding_opens_a_draft_not_a_ready_pr(store):
    """Bypassing the ceiling is not the same as earning the top level. A human
    said "build this", not "and I have already reviewed it"."""
    _record(store, accepted=20, rejected=0)
    action, _ = action_for(
        store, "code-defects", configured_ceiling=3, trust=None, human_authorised=True
    )
    assert action is Action.draft_pr
    assert as_draft(action) is True


# --- the spine routes; a lens never does ----------------------------------------------


def test_only_the_spine_reads_earned_level():
    """This replaces M1b-1's "nothing reads it at all", and the replacement is
    the deliverable.

    `earned_level` may be consulted by the routing module and by the CLI that
    displays it -- never by a lens. The design's load-bearing rule is that a
    lens produces candidates and evidence while everything with consequences
    belongs to the spine; a lens that could read its own earned level could act
    on it.
    """
    src = Path(__file__).resolve().parents[2] / "src" / "whetstone"
    files = sorted(src.rglob("*.py"))
    assert len(files) >= 5, f"the scan is not reaching src/: {files}"

    readers = {
        p.relative_to(src).as_posix()
        for p in files
        if "earned_level" in p.read_text(encoding="utf-8")
    }
    # `readmodel.py` is spine, and it is a DISPLAY path -- the same reason
    # `cli.py` is on this list. The rule forbids a LENS from reading its own
    # earned level, because a lens that can read it can act on it; it has never
    # forbidden the spine from showing it to a human. The read model is where
    # every surface now gets it, so the alternative to this entry is each
    # surface calling `earned_level` for itself, which is more readers, not
    # fewer. The `lenses/` assertion below is the half that actually binds.
    allowed = {
        "queue/autonomy.py",
        "queue/routing.py",
        "cli.py",
        "readmodel.py",
    }
    assert readers <= allowed, readers - allowed
    assert not any(r.startswith("lenses/") for r in readers), (
        "a lens can read its own earned level, and a lens that can read it can "
        "act on it"
    )
    assert "queue/routing.py" in readers, (
        "nothing consults earned_level, so autonomy still enforces nothing -- "
        "which was M1b-1's deliberate state and is not this milestone's"
    )
