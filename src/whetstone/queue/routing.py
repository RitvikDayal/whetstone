"""What happens to a verified finding without a human, and what does not.

THIS IS WHERE AUTONOMY STOPS BEING A NUMBER AND STARTS BEING A GATE. M1b-1
shipped `earned_level` deliberately enforcing nothing, with a test asserting
that nothing outside `queue/` read it, so the absent enforcement stayed visibly
absent rather than looking like an oversight. That test changes here, and
changing it is the deliverable.

THE SPINE ROUTES; A LENS NEVER DOES. `earned_level` is consulted here and
nowhere else, and a lens pack may not reach it -- the design's load-bearing rule
is that a lens produces candidates and evidence while everything with
consequences belongs to the spine. A lens that could read its own earned level
could act on it.

AUTONOMY GOVERNS ONLY WHAT HAPPENS WITHOUT A HUMAN. An explicit `implement`
disposition bypasses the ceiling entirely -- that is the design's section 4.3, and it is
why `implement` exists as a disposition rather than as a level. A human decision
is not autonomy.

THERE IS NO LEVEL 4. `Action.merge` does not exist, `Action` has four members,
and a test asserts the set. The absence is the invariant.
"""

from __future__ import annotations

import sqlite3
from enum import StrEnum

from .autonomy import earned_level


class Action(StrEnum):
    """What the spine may do without asking.

    Four, matching the design's four levels. There is deliberately no `merge`
    and no `deploy`: a pull request is opened and a human takes it from there,
    and `tests/unit/test_invariants.py` fails if a command by either name
    appears anywhere under `src/`.
    """

    report = "report"       # 0 -- it lands in the queue and the report
    propose = "propose"     # 1 -- a spec is written; no code is touched
    draft_pr = "draft_pr"   # 2 -- implemented, verified, opened as a draft
    open_pr = "open_pr"     # 3 -- the same, opened ready for review


_BY_LEVEL = {
    0: Action.report,
    1: Action.propose,
    2: Action.draft_pr,
    3: Action.open_pr,
}


def action_for(
    conn: sqlite3.Connection,
    lens: str,
    *,
    configured_ceiling: int,
    trust: str | None,
    human_authorised: bool = False,
) -> tuple[Action, str]:
    """What may happen to this lens's findings, and the sentence explaining it.

    *human_authorised* is the `implement` disposition: an explicit human
    decision, which does NOT respect the ceiling. That is the design's rule
    rather than a convenience -- a `product-ux` finding hard-capped at level 1
    can still be built when a person hands it back, and it then passes the same
    verification a crash bug does.

    Everything else is bounded by what the lens has EARNED, which is bounded in
    turn by what the user configured. Both bounds apply; the lower wins.
    """
    if human_authorised:
        return Action.draft_pr, (
            f"a human authorised this explicitly, so {lens}'s earned level does "
            "not apply -- an explicit decision is not autonomy. It is built and "
            "verified like any other finding, and opened as a draft."
        )

    level, why = earned_level(conn, lens, configured_ceiling, trust=trust)
    # `min` rather than a lookup on `level` alone: a ceiling above 3 in a config
    # is a typo, not permission for a level that does not exist.
    bounded = max(0, min(level, 3))
    return _BY_LEVEL[bounded], why


def may_publish_a_pull_request(action: Action) -> bool:
    """Whether *action* opens a pull request at all.

    Named rather than written as `action in (...)` at each call site: there are
    two levels that publish and two that do not, and a call site that spelled
    the set itself would be a second place the mapping lives.
    """
    return action in (Action.draft_pr, Action.open_pr)


def as_draft(action: Action) -> bool:
    """Draft at level 2, ready at level 3. The only thing the level changes
    once publication is permitted at all."""
    return action is Action.draft_pr
