"""GitHub sinks: an issue for a finding, a pull request for a verified fix.

NOTHING HERE MERGES, AND THERE IS NO CODE PATH THAT COULD. `gh pr create` is
the whole of what the PR sink does. `tests/unit/test_invariants.py` scans this
file for the forbidden verbs along with everything else in `src/`, so the
absence is mechanical rather than a promise -- and that scan reads raw source
text, so this paragraph may not spell them. It failed on an earlier draft of
this very docstring, which is the guard behaving correctly rather than being
inconvenient.

DRAFT AT LEVEL 2, READY AT LEVEL 3, and nothing at all below 2. That mapping is
the design's autonomy table, and it is the ONLY thing the level changes here --
a sink does not decide whether to publish, it is told.

THE AUTHENTICATED SURFACE IS `gh`, NOT A TOKEN THIS PROCESS HOLDS. Whetstone
never reads a GitHub credential: it shells out to a CLI the user has already
authenticated, so there is no token in this process to leak into a traceback,
an error message or a model's context. The cost is a hard dependency on `gh`
being installed, which `doctor` is the right place to check and which fails
loudly here rather than silently not publishing.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .base import Publication

# A publication that hangs is a run that hangs, and this one talks to a network.
_TIMEOUT = 120

# The levels at which a pull request may be opened, and in which state. Below
# `draft` nothing is published: level 0 reports and level 1 writes a spec, and
# neither is a PR. There is no level 4 -- Whetstone never merges.
_DRAFT_AT = 2
_READY_AT = 3


def _run(argv: list[str], cwd: Path | None = None) -> tuple[int, str, str]:
    """Run `gh`, never through a shell.

    A list, not a flattened string: flattening lets the host shell re-parse
    every quote inside a title or body, and a finding's title is model-authored
    text containing whatever it contains. That defect broke differently on the
    Windows and Ubuntu CI legs the last time this codebase made it.
    """
    try:
        done = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TIMEOUT,
            check=False,
        )
    except FileNotFoundError:
        return 127, "", "the `gh` CLI is not on PATH"
    except subprocess.TimeoutExpired:
        return 124, "", f"`gh` did not answer within {_TIMEOUT}s"
    return done.returncode, done.stdout.strip(), done.stderr.strip()


@dataclass
class GitHubIssues:
    """One issue per finding. Never edits an existing one.

    Deliberately create-only. Updating an issue means matching a finding to it,
    and a wrong match edits somebody else's issue -- the dedupe ledger already
    stops a finding being published twice, and it is the right place for that
    decision because it is the place that knows what "the same finding" means.
    """

    repo: str
    project_root: Path
    kind: str = "github-issues"

    def publish(self, finding: Any, *, dry_run: bool = False) -> Publication:
        title = f"[whetstone] {getattr(finding, 'title', '')}".strip()
        body = _issue_body(finding)
        argv = [
            "gh", "issue", "create",
            "--repo", self.repo,
            "--title", title,
            "--body", body,
        ]
        if dry_run:
            return Publication(
                published=False,
                kind=self.kind,
                reason="dry run: nothing was published",
                detail={"argv": argv},
            )
        code, out, err = _run(argv, cwd=self.project_root)
        if code != 0:
            return Publication(
                published=False,
                kind=self.kind,
                reason=f"`gh issue create` failed ({code}): {err or out}",
                detail={"argv": argv},
            )
        return Publication(published=True, kind=self.kind, url=out.strip())


@dataclass
class GitHubPullRequest:
    """A pull request for a fix that has already been verified.

    REFUSES AN UNVERIFIED FIX, and that refusal is the point of the sink
    existing separately from the issue one. Opening a PR is the most consequential
    thing this tool does, and the gate for it is `Verification.verified`, which
    was computed by replaying the finding's own evidence rather than by anybody
    asserting the fix works.
    """

    repo: str
    project_root: Path
    base_branch: str = "main"
    kind: str = "github-pr"

    def publish(
        self,
        finding: Any,
        *,
        dry_run: bool = False,
        branch: str | None = None,
        verified: bool = False,
        level: int = 0,
        body: str | None = None,
    ) -> Publication:
        if not verified:
            return Publication(
                published=False,
                kind=self.kind,
                reason=(
                    "refusing to open a pull request for a fix that was not "
                    "verified. The evidence was not replayed, so nothing has "
                    "shown the defect is gone."
                ),
            )
        if level < _DRAFT_AT:
            return Publication(
                published=False,
                kind=self.kind,
                reason=(
                    f"the lens is at autonomy {level}, and a pull request needs "
                    f"{_DRAFT_AT}. Nothing was published; the fix is on branch "
                    f"{branch!r} for a human to take."
                ),
                detail={"branch": branch},
            )
        if not branch:
            return Publication(
                published=False,
                kind=self.kind,
                reason="no branch was pushed, so there is nothing to open a "
                "pull request from.",
            )

        argv = [
            "gh", "pr", "create",
            "--repo", self.repo,
            "--base", self.base_branch,
            "--head", branch,
            "--title", f"[whetstone] {getattr(finding, 'title', '')}".strip(),
            "--body", body or _pr_body(finding),
        ]
        # DRAFT UNLESS THE LENS EARNED READY. The one thing the level changes.
        if level < _READY_AT:
            argv.append("--draft")

        if dry_run:
            return Publication(
                published=False,
                kind=self.kind,
                reason="dry run: nothing was published",
                detail={"argv": argv},
            )
        code, out, err = _run(argv, cwd=self.project_root)
        if code != 0:
            return Publication(
                published=False,
                kind=self.kind,
                reason=f"`gh pr create` failed ({code}): {err or out}",
                detail={"argv": argv},
            )
        return Publication(
            published=True,
            kind=self.kind,
            url=out.strip(),
            detail={"draft": level < _READY_AT},
        )


def _evidence(finding: Any) -> dict[str, Any]:
    raw = getattr(finding, "evidence", None)
    if isinstance(raw, dict):
        return raw.get("data") if isinstance(raw.get("data"), dict) else raw
    return {}


def _issue_body(finding: Any) -> str:
    """The finding as decided, not a rewriting of it.

    Every line comes from a stored field. Nothing here calls a model, and the
    grade and its reason are included because a reader who cannot see why this
    survived cannot judge whether to trust it.
    """
    data = _evidence(finding)
    parts = [
        getattr(finding, "detail", "") or "",
        "",
        f"**Subject:** `{getattr(finding, 'subject', '')}`",
        f"**Lens:** {getattr(finding, 'lens', '')}",
        f"**Severity:** {getattr(finding, 'severity', '')}",
    ]
    grade = getattr(finding, "grade", None)
    if grade:
        parts.append(f"**Grade:** {grade} -- {getattr(finding, 'grade_reason', '')}")
    counter = (data.get("falsify") or {}).get("strongest_counterargument")
    if counter:
        parts += ["", "**The strongest case against this finding, which it survived:**",
                  "", f"> {counter}"]
    parts += ["", "---", "", "Opened by whetstone. It never merges and never deploys."]
    return "\n".join(parts)


def _pr_body(finding: Any) -> str:
    data = _evidence(finding)
    parts = [
        f"Fixes the defect at `{getattr(finding, 'subject', '')}`.",
        "",
        getattr(finding, "detail", "") or "",
    ]
    grade = getattr(finding, "grade", None)
    if grade:
        parts += ["", f"**Grade:** {grade} -- {getattr(finding, 'grade_reason', '')}"]
    repro = (data.get("reproduction") or {}).get("verdict")
    if repro:
        parts += ["", f"**Reproduction:** {repro}, run by the controller in a container."]
    parts += [
        "",
        "**Verified before this was opened:** the original reproduction no "
        "longer passes, and the regression test fails without this change and "
        "passes with it.",
        "",
        "---",
        "",
        "Opened by whetstone. It never merges and never deploys -- this is "
        "yours to review.",
    ]
    return "\n".join(parts)


def available() -> str | None:
    """None when `gh` can publish, or the reason it cannot.

    Checked BEFORE anything is attempted, so a run without `gh` says so once
    rather than failing per finding -- and so the reason is the same whether
    the refusal happens here or in `doctor`.
    """
    code, out, err = _run(["gh", "auth", "status"])
    if code == 127:
        return "the `gh` CLI is not on PATH, so nothing can be published to GitHub"
    if code != 0:
        return f"`gh` is not authenticated: {err or out}"
    return None
