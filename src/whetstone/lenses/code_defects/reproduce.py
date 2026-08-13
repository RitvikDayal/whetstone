"""The reproduce stage: the model writes evidence, the controller runs it.

WHY THE CONTROLLER EXECUTES. No M1a stage has a shell -- option A, 2026-08-13 --
so the model cannot run its own reproduction even if we wanted it to. That is
the invariant working rather than a limitation: invariant 2 says a model's
self-assessment is discarded and recomputed from the world, and `reproduced` in
the payload is exactly such an assessment. The exit code overwrites it. Seven
runs on the predecessor project recorded a verdict about a defect none of them
could execute against.

WHAT IS EXECUTED, AND WHAT IS NOT. Only `kind: "pytest"`, and only through the
target's own declared test command. `script` and `command` are refused. The
user already lets Whetstone run their test suite -- `doctor` does it -- so this
adds a file to something they already trust, rather than making Whetstone a way
to run arbitrary text a model produced.

THE EXIT-CODE CONVENTION IS THE OPPOSITE OF A REGRESSION TEST. The artifact
PASSES while the defect is present, because it asserts the broken behaviour. So
exit 0 is the evidence. A failure is then ambiguous -- the defect may be absent,
or the test may be broken -- and those are completely different answers:

    0                       reproduced
    1 with the marker       absent; the assertion that was checking the defect
                            is the thing that failed
    1 without the marker    inconclusive; something else broke, and a broken
                            harness must never read as "the defect is gone"
    5                       inconclusive; pytest collected no tests at all
    anything else           inconclusive

Absence has to be EARNED. Reading any failure as absence is how a tool closes a
real defect on the strength of its own typo.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from string import Template
from typing import Any

from ..._subprocess import run_shell
from ...policy.profiles import profile_for
from ...provider import sentinel
from ...provider.base import Provider, StageRequest
from ...schemas import load_schema
from ..base import RunContext
from .prompts import load_prompt

# Every assertion in the artifact must carry this. See the module docstring:
# it is the only thing that separates "the defect is absent" from "the test is
# broken", and the exit code cannot.
REPRO_MARKER = "WHETSTONE-REPRO"

# The only artifact kind Whetstone will execute. Option A.
_EXECUTABLE_KIND = "pytest"

_TEST_TIMEOUT_SECONDS = 300


def _sanitise(candidate: dict[str, Any]) -> dict[str, Any]:
    """What the reproducer is allowed to see.

    Facts, not opinions. The reproducer gets the observation, where it is, and
    the failure scenario -- and never `root_cause_hypothesis`, `confidence`,
    `severity`, `title` or `alternative_explanations`. Those are the
    discoverer's conclusions, and a reproducer that reads them is being told
    what to find.

    An ALLOW-LIST, so a hypothesis-shaped field added to the hunt schema later
    is excluded by default rather than by somebody remembering to add it to a
    deny-list.
    """
    return {
        "subject": candidate.get("subject"),
        "observation": candidate.get("observation"),
        "failure_scenario": candidate.get("failure_scenario"),
    }


def _prompt_for(candidate: dict[str, Any]) -> str:
    facts = _sanitise(candidate)
    return Template(load_prompt("reproduce")).safe_substitute(
        observation=facts["observation"] or "(none given)",
        subject=facts["subject"] or "(unknown)",
        failure_scenario=facts["failure_scenario"] or "(none given)",
    )


def _verdict_from(returncode: int, output: str) -> tuple[str, str | None]:
    """`(verdict, reason)` for what the controller's own run produced."""
    if returncode == 0:
        return "reproduced", None
    if returncode == 5:
        return (
            "inconclusive",
            "the reproduction collected no tests at all, so nothing was checked",
        )
    if returncode == 1 and REPRO_MARKER in output:
        return "absent", None
    if returncode == 1:
        return (
            "inconclusive",
            f"the reproduction failed without the {REPRO_MARKER} marker, so it "
            f"is a broken test rather than evidence the defect is gone",
        )
    return (
        "inconclusive",
        f"the reproduction exited {returncode}, which settles nothing",
    )


def reproduce(
    candidate: dict[str, Any],
    ctx: RunContext,
    provider: Provider,
    test_command: str,
) -> tuple[dict[str, Any], list[str]]:
    """Ask for a reproduction, then run it and believe the run.

    *test_command* is passed in rather than read from config: `RunContext` is
    deliberately "everything a lens is allowed to know" and does not carry the
    project config, and reaching around it to load `whetstone.yaml` inside a
    lens would duplicate the loader and bypass that boundary. Task 9's pack has
    the config and supplies it.
    """
    skips: list[str] = []
    result = provider.run_stage(
        StageRequest(
            stage="reproduce",
            prompt=_prompt_for(candidate),
            schema=load_schema("reproduce"),
            permissions=profile_for("reproduce"),
            effort=str(ctx.options.get("effort", "medium")),
            max_budget_usd=None,
            cwd=ctx.project_root,
        )
    )

    outcome: dict[str, Any] = {
        "reproduced": False,
        "verdict": "inconclusive",
        "has_runnable_artifact": False,
        "mutation": result.mutation,
        "payload": result.data,
        "provenance": {
            "turns": result.turns,
            "cost_usd": result.usage.cost_usd,
            "tokens": result.usage.total_tokens,
        },
    }

    if result.mutation:
        skips.append(f"reproduce modified the worktree: {result.mutation}")
        return outcome, skips
    if not result.ok or result.data is None:
        skips.append(
            f"reproduce did not run: "
            f"{result.error or 'the provider failed without saying why'}"
        )
        return outcome, skips

    artifact = result.data.get("artifact")
    if artifact is None:
        # The prompt tells the model to say so rather than fabricate a test.
        # That is an honest answer and cannot also be a skip -- the stage ran.
        outcome["verdict"] = "not attempted"
        return outcome, skips

    kind = artifact.get("kind")
    if kind != _EXECUTABLE_KIND:
        skips.append(
            f"reproduce returned a {kind!r} artifact, which Whetstone will not "
            f"execute -- only {_EXECUTABLE_KIND} runs, through the project's own "
            f"test command. The finding carries no runnable evidence."
        )
        return outcome, skips

    content = artifact.get("content") or ""
    if not content.strip():
        skips.append("reproduce returned an empty artifact, so there was nothing to run")
        return outcome, skips

    outcome["has_runnable_artifact"] = True

    # A name nothing else will collide with, at the project root so the
    # project's own test command finds it and its imports resolve the way the
    # project's do.
    path = ctx.project_root / f"test_whetstone_repro_{uuid.uuid4().hex[:12]}.py"
    before = sentinel.fingerprint(ctx.project_root)
    try:
        path.write_text(content, encoding="utf-8")
        shell = run_shell(
            f'{test_command} "{path.name}"',
            ctx.project_root,
            _TEST_TIMEOUT_SECONDS,
            # No bytecode. Without this, running the artifact leaves a `.pyc`
            # for it in `__pycache__` -- a file Whetstone put in the user's
            # repository and did not remove, which the sentinel then reports as
            # a mutation, correctly. Preventing the write beats cleaning up
            # after it: there is nothing to miss.
            env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
        )
    finally:
        # `missing_ok` rather than a guard: a reproduction that deleted its own
        # file is a mutation the sentinel will report, and failing to clean up
        # here would leave the repository dirtier than we found it.
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)

    if shell.timed_out:
        outcome["verdict"] = "inconclusive"
        skips.append(
            f"the reproduction did not finish within {_TEST_TIMEOUT_SECONDS}s and "
            f"was killed, so it settles nothing"
        )
    else:
        verdict, reason = _verdict_from(shell.returncode, shell.output)
        outcome["verdict"] = verdict
        outcome["reproduced"] = verdict == "reproduced"
        if reason:
            skips.append(f"reproduce: {reason}")

    # AROUND THE CONTROLLER'S OWN RUN, not only around the model's. The
    # artifact's own path is excluded because we wrote it deliberately; a
    # reproduction that touched anything else is a finding about the
    # reproduction, whatever it concluded about the code.
    mutation = sentinel.assert_unchanged(ctx.project_root, before)
    if mutation and path.name not in mutation:
        outcome["mutation"] = mutation
        skips.append(f"the reproduction modified the worktree while it ran: {mutation}")

    return outcome, skips
