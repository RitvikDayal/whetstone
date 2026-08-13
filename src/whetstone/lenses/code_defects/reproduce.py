"""The reproduce stage: the model writes evidence, the controller runs it.

WHY THE CONTROLLER EXECUTES. No M1a stage has a shell -- option A, 2026-08-13 --
so the model cannot run its own reproduction even if we wanted it to. That is
the invariant working rather than a limitation: invariant 2 says a model's
self-assessment is discarded and recomputed from the world, and `reproduced` in
the payload is exactly such an assessment. The exit code overwrites it. Seven
runs on the predecessor project recorded a verdict about a defect none of them
could execute against.

WHAT IS EXECUTED, AND WHERE. Only `kind: "pytest"`, only through the target's
own declared test command, and only INSIDE A CONTAINER.

The container is not belt and braces. `kind: "pytest"` bounds what invokes the
artifact and nothing whatever about what the artifact can do -- a pytest file
is an arbitrary Python program. The first version of this module leaned on that
distinction and was wrong about it. `sandbox.py` supplies the boundary the
policy gate cannot: no network, one mount, dropped capabilities.

NO SANDBOX, NO EXECUTION. An unconfigured image, a missing Docker, or a daemon
that will not answer all produce the same outcome -- the artifact is not run,
the reason reaches the user, and the finding caps at grade B under invariant 3.
That is checked BEFORE the artifact is written, so a refusal leaves nothing
behind.

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

WHAT REMAINS UNBOUNDED, stated so nobody reads the container as total: the
artifact still reads and writes the worktree, which is the point of running it,
so the sentinel is still the thing that reports what it touched there. A
container is not a boundary against a kernel exploit. And the image belongs to
the user -- Whetstone knows it is the one they named and nothing else about it.
"""

from __future__ import annotations

import contextlib
import uuid
from pathlib import Path
from string import Template
from typing import Any
from xml.etree import ElementTree

from ...policy.profiles import profile_for
from ...provider import sentinel
from ...provider.base import Provider, StageRequest
from ...sandbox import availability, run_sandboxed
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
    sandbox_image: str | None = None,
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

    # CHECKED BEFORE ANYTHING IS WRITTEN. No sandbox means the artifact is not
    # run at all -- not run unsandboxed, and not written and then abandoned.
    # `kind: "pytest"` bounds what invokes the artifact and nothing about what
    # it can do, so the container is the only thing standing between a model's
    # output and the user's machine.
    blocked = availability(sandbox_image)
    if blocked is not None:
        outcome["verdict"] = "not executed"
        skips.append(f"the reproduction was not run: {blocked.reason}")
        return outcome, skips

    # A name nothing else will collide with, at the project root so the
    # project's own test command finds it and its imports resolve the way the
    # project's do.
    stem = f"whetstone_repro_{uuid.uuid4().hex[:12]}"
    path = ctx.project_root / f"test_{stem}.py"
    # Inside the worktree, because that is the container's only writable
    # location -- and removed again below, so it is not a file left in the
    # user's repository.
    report = ctx.project_root / f"{stem}.xml"
    before = sentinel.fingerprint(ctx.project_root)
    try:
        path.write_text(content, encoding="utf-8")
        # The report goes to the worktree because that is the container's only
        # writable location, and is read and removed from the host immediately
        # afterwards. No defensive quoting: the argv reaches docker without a
        # host shell, so the container's own `sh -lc` is the only thing that
        # parses this string.
        inner = f"{test_command} {path.name} --junit-xml={report.name}"
        shell = run_sandboxed(
            inner,
            ctx.project_root,
            sandbox_image or "",
            _TEST_TIMEOUT_SECONDS,
            # Through docker's own `-e` rather than a command prefix: a prefix
            # binds to one simple command, so a compound `test_command` would
            # apply it to the first part only and pytest would leave bytecode
            # in the mounted worktree.
            env={"PYTHONDONTWRITEBYTECODE": "1"},
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
