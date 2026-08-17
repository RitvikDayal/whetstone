"""The implement stage: the first stage in this project permitted to write.

WHAT CHANGES HERE, AND WHAT DOES NOT. Every stage before this one held `Read`,
`Grep`, `Glob` and nothing else -- D21, "no M1a stage gets a shell". An
implementer that cannot write cannot implement, so this stage holds `Edit` and
`Write` too, scoped by `--add-dir` to the run's worktree. `Bash`, `Agent` and
`TaskCreate` stay forbidden, and that is what keeps the scoping meaningful: a
shell can write anywhere, and a subagent is an unbounded tool set reachable
from a bounded one.

THE WORKTREE IS THE BLAST RADIUS, NOT THE PROJECT. `cwd` is the worktree, the
write root is the worktree, and the sentinel fingerprints the worktree. The
user's checkout is measured separately by the caller and must come back
unchanged; nothing in this module can see it, which is the point.

THE MUTATION CHECK INVERTS HERE, AND IT IS THE ONE PLACE IN THIS CODEBASE THAT
DOES. Every other stage discards its result if the worktree moved. This stage
is asked to move it, so an EMPTY mutation is the failure: a stage that reported
a fix and changed nothing has produced a summary about work it did not do. The
sentinel is still the authority -- `changed_files` from the payload is a claim,
and a claim about what a model touched is exactly what invariant 2 says is
recomputed from the world rather than believed.

A FIX WITHOUT A REGRESSION TEST IS NOT ACTED ON. The schema allows
`regression_test: null` because an honest refusal is worth more than a change
nobody can re-check, and invariant 3 -- evidence must be executable -- applies
to the fix as much as to the reproduction. The stage returns it, the verifier
refuses it, and the finding stays where it was.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from string import Template
from typing import Any

from ...policy.profiles import implement_profile
from ...provider.base import Provider, StageRequest
from ...schemas import load_schema
from ..base import RunContext
from .prompts import load_prompt

# Same ceiling the sentinel uses. A git call that hangs is a run that hangs.
_GIT_TIMEOUT = 60


def changed_files(worktree: Path) -> list[str]:
    """What actually moved in *worktree*, asked of git rather than of a model.

    `--porcelain -z` because the newline form C-quotes awkward paths and a
    non-ASCII filename then arrives as an octal escape -- the same reason
    `sentinel.py` uses `-z`. `--untracked-files=all` because a new file is the
    ordinary shape of a fix that adds a regression test, and the default
    collapses a new directory to its name.

    Never raises: this runs after a model has been writing, so a path it just
    deleted or made unreadable must not take the run down. An empty list from a
    failed git call is indistinguishable from an empty list from a clean tree,
    which is why the CALLER checks `result.mutation` for whether anything moved
    and uses this only for what.
    """
    completed = subprocess.run(
        ["git", "--no-optional-locks", "status", "--porcelain", "-z",
         "--untracked-files=all"],
        cwd=worktree,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=_GIT_TIMEOUT,
        check=False,
    )
    if completed.returncode != 0:
        return []
    # Each record is "XY <path>"; the status letters are two columns plus a
    # space, and a rename carries a second NUL-separated path which is dropped
    # here because the destination is the file that exists now.
    return sorted(
        record[3:]
        for record in completed.stdout.split("\0")
        if len(record) > 3
    )


def _reproduction_text(reproduction: dict[str, Any]) -> str:
    """What the controller established, in words, not the model's claim.

    `verdict` came from an exit code and travels; `payload["reproduced"]` is
    the reproducer's opinion of itself and does not. The artifact travels
    because the implementer has to know what it must not break.
    """
    verdict = str(reproduction.get("verdict") or "unknown")
    executed = bool(reproduction.get("executed"))
    lines = [
        f"The controller ran the reproduction itself and recorded: {verdict}."
        if executed
        else f"No reproduction was executed. Recorded outcome: {verdict}.",
    ]
    artifact = reproduction.get("artifact") or {}
    content = str(artifact.get("content") or "").strip()
    if content:
        lines.append(
            "The reproduction artifact is below. It PASSES while the defect is "
            "present, so your fix should make it fail. Do not edit or delete "
            "it.\n\n```python\n" + content + "\n```"
        )
    return "\n\n".join(lines)


def _substitutions(
    candidate: dict[str, Any],
    reproduction: dict[str, Any],
    verdict: dict[str, Any],
) -> dict[str, str]:
    """Exactly the facts the implementer needs, named explicitly.

    An allow-list, like `falsify._sanitise`: a field added to the hunt schema
    later is excluded by default rather than by somebody remembering. The
    implementer DOES get the hypothesis, unlike the falsifier -- it is fixing
    the defect, not judging it, and withholding the cause from the thing asked
    to fix the cause would be theatre.
    """
    return {
        "observation": str(candidate.get("observation") or ""),
        "subject": str(candidate.get("subject") or ""),
        "reproduction": _reproduction_text(reproduction),
        "counterargument": str(verdict.get("strongest_counterargument") or ""),
    }


def _prompt_for(
    candidate: dict[str, Any],
    reproduction: dict[str, Any],
    verdict: dict[str, Any],
) -> str:
    return Template(load_prompt("implement")).substitute(
        _substitutions(candidate, reproduction, verdict)
    )


def implement(
    candidate: dict[str, Any],
    reproduction: dict[str, Any],
    verdict: dict[str, Any],
    worktree: Path,
    ctx: RunContext,
    provider: Provider,
) -> tuple[dict[str, Any], list[str]]:
    """Write a fix in *worktree*. Returns the outcome and any skips.

    The outcome's `changed_files` comes from the SENTINEL, not the payload.
    """
    skips: list[str] = []
    result = provider.run_stage(
        StageRequest(
            stage="implement",
            prompt=_prompt_for(candidate, reproduction, verdict),
            schema=load_schema("implement"),
            permissions=implement_profile(worktree),
            effort=str(ctx.options.get("effort", "medium")),
            # Run-level, like every other stage. A per-stage ceiling low enough
            # to bound anything is low enough to make the stage a no-op.
            max_budget_usd=None,
            # The WORKTREE, not the project root. This is the only stage where
            # those differ, and it is the whole safety story.
            cwd=worktree,
        )
    )

    outcome: dict[str, Any] = {
        # The safe direction on every path below: nothing was implemented until
        # something says otherwise.
        "implemented": False,
        "changed_files": [],
        "summary": None,
        "regression_test": None,
        "notes": None,
        "mutation": result.mutation,
        "payload": result.data,
        "provenance": {
            "turns": result.turns,
            "cost_usd": result.usage.cost_usd,
            "tokens": result.usage.total_tokens,
        },
    }

    if result.denials:
        skips.append(
            f"implement was refused {', '.join(sorted(set(result.denials)))} "
            "and its change was discarded -- a partial fix is worse than none, "
            "because it looks like a fix."
        )
        return outcome, skips
    if not result.ok:
        skips.append(
            f"implement did not run: "
            f"{result.error or 'the provider failed without saying why'}"
        )
        return outcome, skips
    if result.data is None:
        skips.append(
            "implement returned success with no payload, so there is nothing "
            "to read."
        )
        return outcome, skips

    # INVERTED, and deliberately. Everywhere else a mutation discards the
    # result; here its ABSENCE does. A stage that returns a summary describing
    # a fix and left the worktree untouched has described work it did not do,
    # and that is the failure mode a prose payload hides best.
    if not result.mutation:
        skips.append(
            "implement reported a change and the worktree is untouched, so "
            "nothing was written. The finding is left as it was."
        )
        return outcome, skips

    regression = result.data.get("regression_test")
    if not regression:
        skips.append(
            "implement wrote no regression test, so its fix cannot be "
            "re-checked and will not be verified or published. Reason given: "
            f"{result.data.get('notes') or 'none'}"
        )
        return outcome, skips

    outcome["implemented"] = True
    # RE-DERIVED FROM GIT, not read off the payload. `changed_files` in the
    # payload is a claim about what a model touched, and a stage
    # under-reporting its own writes is precisely what must not be taken on
    # trust. Both are kept so the disagreement is visible rather than resolved
    # silently in the model's favour.
    outcome["changed_files"] = changed_files(worktree)
    outcome["claimed_files"] = sorted(
        str(p) for p in result.data.get("changed_files") or []
    )
    unclaimed = set(outcome["changed_files"]) - set(outcome["claimed_files"])
    if unclaimed:
        skips.append(
            "implement changed files it did not report: "
            f"{', '.join(sorted(unclaimed))}. The change is kept and the "
            "discrepancy is recorded; a reviewer reads the diff, not the list."
        )
    outcome["summary"] = result.data.get("summary")
    outcome["regression_test"] = regression
    outcome["notes"] = result.data.get("notes")
    return outcome, skips
