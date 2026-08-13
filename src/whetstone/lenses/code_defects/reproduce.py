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

    0                        reproduced
    1, one test, and the      absent; the assertion that was checking the defect
      marker in ITS failure   is the thing that failed
    1, anything else          inconclusive; a broken harness must never read as
                              "the defect is gone"
    5                         inconclusive; pytest collected no tests at all
    anything else             inconclusive

Absence has to be EARNED. Reading any failure as absence is how a tool closes a
real defect on the strength of its own typo.

AND THE MARKER IS BOUND TO THE FAILING TEST, not searched for in the output.
Scanning stdout let the artifact PRINT the marker and then fail an unrelated
assertion: exit 1 plus the string anywhere read as absence, while the predicate
that was supposed to check the defect went unchecked. `--junit-xml` is written
by pytest rather than by the model, so the failure it attributes is one the
controller owns.

WHAT THIS MODULE STILL DOES NOT BOUND, and it is the important gap: the
artifact is arbitrary Python. `kind: "pytest"` restricts what INVOKES it, not
what it can do -- a pytest file can write outside the worktree, touch `.git`,
or spawn `git push`. The permission profile bounds the provider stage and not
this, and the sentinel reports mutations only after the fact and only inside
`project_root`. Closing it needs an OS-enforced boundary, which is not M1a's.
See the M1a plan's execution decision.
"""

from __future__ import annotations

import contextlib
import os
import uuid
from pathlib import Path
from string import Template
from typing import Any
from xml.etree import ElementTree

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


def _bound_failure(report: Path) -> tuple[int, str | None] | None:
    """`(test count, the failure message of the single test)` from pytest's own
    JUnit report, or None if there is no usable report.

    CONTROLLER-OWNED, which is the whole point. Searching stdout for the marker
    let the artifact PRINT `WHETSTONE-REPRO` and then fail an unrelated
    assertion: return code 1 plus the string anywhere in the output read as
    `absent`, although the predicate that was supposed to check the defect had
    not been checked at all. The marker has to be bound to the failure of the
    test, and pytest writes this file rather than the model.
    """
    try:
        root = ElementTree.parse(report).getroot()
    except (OSError, ElementTree.ParseError):
        return None
    cases = root.iter("testcase")
    messages: list[str | None] = []
    for case in cases:
        failure = case.find("failure")
        if failure is None:
            messages.append(None)
            continue
        # THE `message` ATTRIBUTE ONLY, never the element text. pytest puts the
        # assertion message in the attribute and the TRACEBACK in the text --
        # and the traceback quotes the test's own source, which the model
        # wrote. Including it let an artifact that merely PRINTS the marker,
        # or contains it in a comment, buy absence on an unrelated failure.
        messages.append(failure.get("message") or "")
    if not messages:
        return None
    failures = [m for m in messages if m is not None]
    if len(failures) != 1:
        # Zero failures is not this branch's business; more than one means the
        # marker cannot be attributed to the reproduction assertion.
        return len(messages), None
    return len(messages), failures[0]


def _verdict_from(
    returncode: int, bound: tuple[int, str | None] | None
) -> tuple[str, str | None]:
    """`(verdict, reason)` for what the controller's own run produced."""
    if returncode == 0:
        return "reproduced", None
    if returncode == 5:
        return (
            "inconclusive",
            "the reproduction collected no tests at all, so nothing was checked",
        )
    if returncode == 1:
        if bound is None:
            return (
                "inconclusive",
                "the reproduction failed and pytest produced no report to attribute "
                "the failure to, so absence cannot be established",
            )
        count, failure = bound
        if count != 1:
            return (
                "inconclusive",
                f"the reproduction contained {count} tests rather than one, so a "
                f"failure cannot be attributed to the defect predicate",
            )
        if failure is None or REPRO_MARKER not in failure:
            return (
                "inconclusive",
                f"the reproduction failed without the {REPRO_MARKER} marker in the "
                f"failing assertion, so it is a broken test rather than evidence "
                f"the defect is gone",
            )
        return "absent", None
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
    stem = f"whetstone_repro_{uuid.uuid4().hex[:12]}"
    path = ctx.project_root / f"test_{stem}.py"
    # The report goes OUTSIDE the worktree: it is Whetstone's bookkeeping, not
    # the project's, and writing it inside would be a file we put in the user's
    # repository for the sentinel to find.
    report = ctx.state_root / f"{stem}.xml"
    report.parent.mkdir(parents=True, exist_ok=True)
    before = sentinel.fingerprint(ctx.project_root)
    try:
        path.write_text(content, encoding="utf-8")
        shell = run_shell(
            f'{test_command} "{path.name}" --junit-xml="{report}"',
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
        bound = _bound_failure(report)
        with contextlib.suppress(OSError):
            report.unlink(missing_ok=True)

    if shell.timed_out:
        outcome["verdict"] = "inconclusive"
        skips.append(
            f"the reproduction did not finish within {_TEST_TIMEOUT_SECONDS}s and "
            f"was killed, so it settles nothing"
        )
    else:
        verdict, reason = _verdict_from(shell.returncode, bound)
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
