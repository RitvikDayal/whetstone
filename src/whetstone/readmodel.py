"""One projection of the store, rendered by every surface.

WHY THIS EXISTS RATHER THAN EACH SURFACE READING THE STORE ITSELF. Every
surface this project has shipped has, at least once, computed the right answer
and then lost it on the way to the screen:

- M1a's falsifier verdict was correct and never reached the list a user reads.
- A grade-D finding rendered identically to a grade A in `whetstone findings`,
  so the one column that says the gate worked was invisible.
- `get_last_run` selected the row and dropped `status`, so an interrupted run
  rendered as a clean report.

A control plane is a second chance to make all three mistakes, with more
surface area to hide them behind. So the derived facts -- is this killed, was
it graded at all, did the run finish -- are computed ONCE, here, as fields.
A surface may choose not to render a field. It may not compute a different
answer.

EVERY DERIVED BOOLEAN IS EXPLICIT, and that is the point rather than a style.
`grade is None` and `grade == "D"` are different facts about a finding and the
store's own schema comment says so: absent means the lens did not grade it,
NOT that it graded it badly. A consumer that reads `not view["grade"]` gets
the wrong answer for exactly one of those, so neither surface is trusted to
derive it.

JSON-SAFE, and no framework import. Everything returned here survives
`json.dumps` unchanged, so the HTTP layer adds no encoding step where a value
could be reinterpreted -- and this module stays importable by the CLI, which
must not need a web server installed to print a table.
"""

from __future__ import annotations

import json
import math
import sqlite3
from pathlib import Path
from typing import Any

from .config.model import WhetstoneConfig
from .queue.autonomy import earned_level
from .queue.decisions import acceptance_rate
from .runner import RunResult, get_last_run
from .store.findings import Finding, list_findings

# The prefix `findings` prints and `decide` accepts. Defined here rather than
# imported from `cli.py`, which would make the web layer import Typer; the CLI
# imports it from here instead, so the two cannot disagree about how long a
# short id is.
ID_PREFIX = 8


def finding_view(finding: Finding) -> dict[str, Any]:
    """One finding, with every derived fact already decided.

    `killed` and `graded` are separate booleans on purpose -- see the module
    docstring. `killed` is true only for D. `graded` is false only when the
    lens returned no grade at all, which `hygiene` always does and which is not
    a verdict about the finding.
    """
    return {
        "id": finding.id,
        "short_id": finding.id[:ID_PREFIX],
        "dedupe_key": finding.dedupe_key,
        "lens": finding.lens,
        "rule_id": finding.rule_id,
        "subject": finding.subject,
        "title": finding.title,
        "detail": finding.detail,
        "severity": finding.severity,
        "evidence": finding.evidence,
        "state": finding.state,
        "grade": finding.grade,
        "grade_reason": finding.grade_reason,
        # The word, not the letter. `cli.py` learned this the hard way: a
        # letter in a column is a distinction a skimming reader does not make.
        # Carried as a field so a second surface cannot decide to drop it and
        # still claim it renders the verdict.
        "killed": finding.grade == "D",
        "graded": finding.grade is not None,
        "first_seen_run": finding.first_seen_run,
        "last_seen_run": finding.last_seen_run,
        "created_at": finding.created_at,
        "updated_at": finding.updated_at,
    }


def findings_view(
    conn: sqlite3.Connection,
    *,
    state: str | None = None,
    lens: str | None = None,
    grade: str | None = None,
) -> list[dict[str, Any]]:
    """The queue, in the store's own order.

    THE ORDER IS NOT RE-DERIVED HERE and must not be re-derived downstream.
    `store/findings.py` argues its ordering at length -- grade first because
    the grade is what survived the gate and the severity is what the model
    claimed about its own finding, with an ABSENT grade ranking between B and
    C so a measured CVE is neither buried under something the falsifier
    refuted nor ranked above a proven crash. A surface that sorts by severity
    because that column looks more important silently inverts that decision.
    """
    return [finding_view(row) for row in list_findings(conn, state=state, lens=lens, grade=grade)]


def run_view(conn: sqlite3.Connection) -> dict[str, Any] | None:
    """The most recent run, or None when this project has never had one.

    None is a distinct state from "a run that recorded nothing" and every
    consumer has to keep them apart: the first means nothing has been checked,
    the second means something was checked and was clean. Rendering both as
    silence is how a project with no runs reads as a project with no problems.
    """
    return run_result_view(get_last_run(conn))


def run_result_view(run: RunResult | None) -> dict[str, Any] | None:
    """Project a RunResult that is already in hand.

    PUBLIC, because `run_view` reads the LAST run out of the store and that is
    not the only run anyone holds. `execute_run` returns a live result whose
    `lens_count` is set -- the one field `get_last_run` cannot reconstruct,
    because the `runs` table has no column for it -- so a caller with the live
    object must be able to project it without a round trip that would lose the
    very field it has.
    """
    if run is None:
        return None
    return {
        "run_id": run.run_id,
        "tier": run.tier,
        "file_count": run.file_count,
        "status": run.status,
        # Derived here so no surface has to know that "complete" is the one
        # status that counts as finished.
        "finished": run.finished,
        "new": run.new,
        "seen": run.seen,
        "skips": list(run.skips),
        "lens_count": run.lens_count,
    }


def trust_view(
    conn: sqlite3.Connection, config: WhetstoneConfig
) -> list[dict[str, Any]]:
    """What each configured lens has earned, and the sentence explaining it.

    THE SENTENCE IS A FIELD, not decoration. `autonomy.earned_level` returns it
    because the design's claim is that "is this tool trustworthy here" becomes a
    number instead of a feeling, and a number without its reason is still a
    feeling. A surface that renders the integer and drops the string has
    undone the feature while appearing to ship it.

    `rate` travels with `sample` and is None rather than 0.0 when there are no
    decisions -- `decisions.py` refuses to hand back a bare float for the same
    reason, and flattening it here would reintroduce the claim that everything
    was rejected.
    """
    views: list[dict[str, Any]] = []
    for name, lens_config in sorted(config.lenses.items()):
        level, reason = earned_level(
            conn,
            name,
            lens_config.autonomy,
            trust=None if lens_config.trust is None else str(lens_config.trust),
        )
        rate, sample = acceptance_rate(conn, name)
        views.append(
            {
                "lens": name,
                "enabled": lens_config.enabled,
                "configured_ceiling": lens_config.autonomy,
                "earned_level": level,
                "reason": reason,
                "acceptance_rate": rate,
                "sample": sample,
            }
        )
    return views


def cost_view(state_root: Path) -> dict[str, Any]:
    """Recorded model spend, per run and per stage.

    READ OFF DISK RATHER THAN OUT OF A TABLE, and the reason is the schema
    gate: `store/db.py` refuses to open a database stamped with a different
    `user_version` and states there is no migration path, so adding a `costs`
    table would turn this screen into data loss for every existing install.
    `budget.write_cost_record` already writes these files and already gave that
    same reason.

    WHAT ABSENCE MEANS, and why it is a list rather than a sentence. The first
    version of this returned a constant string warning that an absent lens is
    not a free one -- true on every render, whether or not anything was
    actually missing, which is a caption that always fires and therefore
    carries no information. This project's own rule: a health check that
    reports known-benign items trains you to ignore it.

    `budget.write_cost_record` now writes a record whenever a lens built a
    budget at all, including one that spent nothing. So a lens present in
    `lenses_with_records` is fully accounted for, and a lens that ran without
    appearing there declined before it reached a model -- which every such path
    already reports as a skip on the run. That makes the gap nameable instead
    of perpetually possible.
    """
    directory = state_root / "costs"
    records: list[dict[str, Any]] = []
    unreadable: list[str] = []

    if directory.is_dir():
        # Sorted so the view is stable across filesystems; `iterdir` order is
        # not defined and a screen that reorders itself between refreshes reads
        # as data changing when nothing has.
        for path in sorted(directory.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                # Named, not skipped. An unreadable cost record is spend that
                # happened and cannot be shown, which is exactly the thing this
                # view exists to stop being invisible.
                unreadable.append(f"{path.name}: {exc}")
                continue
            if isinstance(record, dict):
                records.append(record)
            else:
                unreadable.append(
                    f"{path.name}: expected a JSON object, found "
                    f"{type(record).__name__}"
                )

    # MALFORMED FIELDS ARE NAMED, not quietly read as zero. These files are
    # JSON on disk and nothing revalidates them between runs, so a truncated
    # write or a hand edit can leave `"spent_usd": null` -- and coercing that
    # to 0.0 in silence turns real spend into $0.00 on the one screen whose
    # entire job is to show what was spent. Same under-count `Budget.spend`
    # already refuses to make one layer down.
    total_usd = 0.0
    total_calls = 0
    total_unmeasured = 0
    for record in records:
        where = str(record.get("run_id") or "a cost record")
        for field, add in (
            ("spent_usd", "usd"),
            ("calls", "calls"),
            ("unmeasured_calls", "unmeasured"),
        ):
            value = _number(record.get(field))
            if value is None:
                unreadable.append(
                    f"{where}: `{field}` is {record.get(field)!r}, which is not "
                    f"a number. It is NOT counted as zero -- the totals below "
                    f"are short by an unknown amount."
                )
                continue
            if add == "usd":
                total_usd += value
            elif add == "calls":
                total_calls += int(value)
            else:
                total_unmeasured += int(value)

    return {
        "records": records,
        "total_usd": total_usd,
        "total_calls": total_calls,
        # Surfaced at the top level rather than buried per record: a ceiling
        # enforced against a total known to be short is the single most
        # important thing a cost screen can say, and it is true of the whole
        # view rather than of one run.
        "unmeasured_calls": total_unmeasured,
        "unreadable": unreadable,
        # Sorted and deduplicated. The surface renders these as "accounted
        # for"; anything a run reports having executed that is NOT here spent
        # nothing because it never reached a model, and said so in a skip.
        "lenses_with_records": sorted(
            {str(r["lens"]) for r in records if isinstance(r.get("lens"), str)}
        ),
    }


def _number(value: Any) -> float | None:
    """*value* as a float, or None when it is not a number.

    `None` RATHER THAN 0.0, and the difference is the whole point: a cost
    record is JSON on disk that nothing revalidates between runs, and reading a
    malformed `spent_usd` as zero reports money that was spent as money that
    was not. The caller names it instead. A bad file must not take down the
    screen that would have shown the damage, and it must not be silent either.

    `bool` is excluded because `True` is an `int` in Python, and summing it as
    1.0 would invent a dollar.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    # NON-FINITE IS NOT A NUMBER HERE. `json.loads` accepts the JavaScript
    # spellings `Infinity`, `-Infinity` and `NaN` by default, so a hand-edited
    # cost record turns `total_usd` into `inf` or `nan` -- and `json.dumps`
    # then emits those same non-standard tokens, which the browser's
    # `JSON.parse` refuses, taking the whole cost screen down rather than one
    # row. Treated as unreadable, which is what it is.
    if not math.isfinite(number):
        return None
    return number
