"""What autonomy a lens has EARNED on this project, and why.

THIS ENFORCES NOTHING, DELIBERATELY. No writer exists until M1b-2, so a level
above 1 has nothing to authorise -- there is no worktree, no implementer, no
PR sink. Shipping the computation first means the number is visible and
calibrating against real decisions before it is allowed to act on any of them.
A test asserts that nothing outside this package reads `earned_level`, so the
absent enforcement stays visibly absent rather than looking like an oversight.

The design's rule: config sets a CEILING, and the engine acts at 0-1 until a
lens has a track record on this project, then promotes to that ceiling. Falling
back below the threshold demotes it, with a stated reason.

The thresholds below are HYPOTHESES TO RECALIBRATE, not tuned constants. They
are named so re-tuning one is an edit rather than a hunt, and the numbers came
from the design doc, not from data -- there is no data yet.
"""

from __future__ import annotations

import sqlite3

from .decisions import ACCEPTANCES, COUNTED, acceptance_rate, decisions_for

# A lens on probation acts at report/propose only, whatever its ceiling says.
PROBATION_LEVEL = 1

# Promotion reads the WHOLE record: a sustained track record is the thing being
# established, and a short window would let three good decisions promote a lens
# that has been wrong twenty times.
PROMOTION_DECISIONS = 10
PROMOTION_RATE = 0.60

# Demotion reads a TRAILING window, and the asymmetry is the point. Promotion
# needs a sustained record; demotion needs to react to a recent collapse. One
# window for both makes one of the two wrong -- with a whole-record rule, a lens
# with 200 good decisions could be wrong every time for a month and keep its
# level.
TRAILING_WINDOW = 10
DEMOTION_RATE = 0.40

# The classification is imported, not restated. This module had its own copy
# with a comment promising to keep it in step with `decisions.py` -- a promise
# is not a mechanism, and the two functions would have drifted the first time
# a disposition was added.


def earned_level(
    conn: sqlite3.Connection,
    lens: str,
    configured_ceiling: int,
    *,
    trust: str | None,
) -> tuple[int, str]:
    """The level *lens* has earned, and the sentence explaining it.

    Never returns above *configured_ceiling*. The ceiling is the user's
    decision and a ceiling promotion can exceed is not a ceiling -- so a
    flawless lens configured at 0 stays at 0, and `trust: assumed` on a lens
    capped at 1 still gives 1.

    `trust="assumed"` skips PROBATION and nothing else. It does not skip
    demotion: the assertion of trust was made before any of these decisions
    existed, and a lens whose trailing record has collapsed is one the record
    disagrees with. Reading `assumed` as "never demote" is the obvious wrong
    implementation and there is a test for it.

    The explanation is not decoration. The design's claim is that "is this tool
    trustworthy here" becomes a number rather than a feeling, and a number
    without its reason is still a feeling.
    """
    ceiling = max(0, int(configured_ceiling))
    probation = min(PROBATION_LEVEL, ceiling)

    demoted, why_demoted = _trailing_collapse(conn, lens, probation)
    if demoted:
        return probation, why_demoted

    if trust == "assumed":
        return ceiling, (
            f"{lens} is at level {ceiling}, its configured ceiling, because "
            "trust is set to assumed and probation was skipped."
        )

    rate, sample = acceptance_rate(conn, lens)
    if rate is None:
        return probation, (
            f"{lens} is on probation at level {probation}: no decisions have "
            "been recorded for it on this project yet."
        )

    if sample < PROMOTION_DECISIONS:
        return probation, (
            f"{lens} is on probation at level {probation}: {sample} decision(s) "
            f"recorded, and promotion needs {PROMOTION_DECISIONS}."
        )

    if rate < PROMOTION_RATE:
        return probation, (
            f"{lens} is on probation at level {probation}: {rate:.0%} of "
            f"{sample} decisions were accepted, and promotion needs "
            f"{PROMOTION_RATE:.0%}."
        )

    return ceiling, (
        f"{lens} is at level {ceiling}, its configured ceiling: {rate:.0%} of "
        f"{sample} decisions were accepted, at or above the {PROMOTION_RATE:.0%} "
        "promotion threshold."
    )


def _trailing_collapse(
    conn: sqlite3.Connection, lens: str, probation: int
) -> tuple[bool, str]:
    """Whether the trailing window has fallen below the demotion rate.

    Requires a FULL window before it can fire. Three rejections in a row on a
    young lens is not a collapse, it is three decisions -- and demoting on it
    would mean the first bad afternoon of a lens's life is indistinguishable
    from a sustained failure.
    """
    counted = [
        d for d in decisions_for(conn, lens=lens) if d.disposition in COUNTED
    ]
    if len(counted) < TRAILING_WINDOW:
        return False, ""

    window = counted[-TRAILING_WINDOW:]
    accepted = sum(1 for d in window if d.disposition in ACCEPTANCES)
    rate = accepted / len(window)
    if rate >= DEMOTION_RATE:
        return False, ""
    # `probation`, not PROBATION_LEVEL: with a ceiling of 0 the caller returns
    # 0 and this sentence claimed 1, so the number the user read was not the
    # number they had.
    return True, (
        f"{lens} was demoted to level {probation}: {rate:.0%} of its "
        f"trailing {TRAILING_WINDOW} decisions were accepted, below the "
        f"{DEMOTION_RATE:.0%} demotion threshold."
    )
