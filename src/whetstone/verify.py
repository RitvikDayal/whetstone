"""Pre-merge verification: replay the finding's OWN evidence against the fix.

A GREEN SUITE IS NECESSARY AND NOWHERE NEAR SUFFICIENT. "the tests pass" is
what a fix looks like from the inside, and it is exactly the claim the reproduce
stage exists to refuse. Two things have to be shown, and neither of them is the
suite:

1. **The reproduction must stop passing.** The artifact was written to PASS
   while the defect was present -- that direction is the opposite of a
   regression test and is the whole convention. So after a real fix it must
   FAIL. An artifact that still passes means the defect is still there,
   whatever the implementer wrote in its summary.

2. **The regression test must fail without the fix.** Run it against the
   UNFIXED code and watch it go red, then against the fixed code and watch it
   go green. A regression test that passes either way guards nothing, and this
   project has shipped that defect more often than any other -- a symlink
   regression test that pre-created the leaf directory, a severity test where
   the buggy ordering gave the same answer, an RCE test that passed with the
   hardening removed.

BOTH RUN IN THE CONTAINER, for the reason `reproduce.py` gives: `kind:
"pytest"` bounds what invokes a file and nothing whatever about what the file
can do. The fix is model-written and so is the regression test.

NO SANDBOX, NO VERIFICATION. Not "verified by inspection", not "assumed": the
outcome says `not executed`, the reason reaches the user, and nothing is
published. That is the same fail-closed stance `reproduce.py` takes, and it
matters more here because the next step after verification is opening a PR.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .sandbox import availability, run_sandboxed

# Same ceiling reproduce.py uses for a container run.
_TIMEOUT = 300

# pytest's own exit codes. 0 passed, 1 tests failed, 5 nothing was collected.
_PASSED = 0
_FAILED = 1
_NOTHING_COLLECTED = 5
# 4 is pytest's USAGE ERROR, which is what an unmatched `file::name` produces --
# NOT 5. Measured: `pytest test_x.py::test_typo` on an existing file exits 4,
# and 5 only appears when collection found nothing at all. Reading 4 as "the
# test failed" would report a typo in the test's name as a fix that does not
# work, which sends a reviewer to the wrong place entirely.
_USAGE_ERROR = 4


@dataclass
class Verification:
    """What was established, and what was not.

    `verified` is True only when BOTH checks ran and both said what they had
    to. Every other combination -- including "could not run" -- is False, and
    `reasons` says which.
    """

    verified: bool = False
    reproduction_now_fails: bool | None = None
    regression_fails_without_the_fix: bool | None = None
    regression_passes_with_the_fix: bool | None = None
    executed: bool = False
    reasons: list[str] = field(default_factory=list)


def _run(command: str, worktree: Path, image: str) -> tuple[int, str]:
    result = run_sandboxed(command, worktree, image, _TIMEOUT)
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def _select(test_command: str, path: str, test_name: str | None = None) -> str:
    """A pytest invocation naming ONE test, not the whole suite.

    Selecting the test matters: running everything and reading the exit code
    cannot tell "the regression test failed" from "an unrelated test in this
    project was already failing", and the second would be read as the first.
    """
    target = f"{path}::{test_name}" if test_name else path
    # `-o cache_dir` for the reason reproduce.py carries: without it pytest
    # writes .pytest_cache into the mounted worktree and the change is
    # attributed to the verification.
    return f"{test_command} {target} -o cache_dir=/tmp/whetstone/pytest-cache"


def verify(
    worktree: Path,
    *,
    reproduction_artifact: str | None,
    regression_test: dict[str, Any] | None,
    changed_files: list[str],
    test_command: str,
    image: str | None,
) -> Verification:
    """Replay the evidence. Returns what was established, never a bare bool.

    *changed_files* is what the implementer actually touched, taken from git
    rather than from its payload -- it is needed to revert the fix for check 2,
    and reverting what a model SAYS it changed would leave the fix in place
    whenever it under-reported.
    """
    outcome = Verification()

    # INPUT BEFORE ENVIRONMENT. A missing regression test is a fact about what
    # was handed over; no Docker is a fact about the machine. Checking the
    # environment first told a user with neither to go install Docker, which
    # would not have helped -- there was nothing to run either way.
    if not regression_test:
        outcome.reasons.append(
            "nothing was verified: the implementer wrote no regression test, "
            "so there is nothing that would catch this defect coming back."
        )
        return outcome

    blocked = availability(image)
    if blocked is not None:
        outcome.reasons.append(
            f"nothing was verified: {blocked.reason}. The fix is not published "
            "-- an unverified fix is a claim, and the next step after this is "
            "opening a pull request."
        )
        return outcome

    outcome.executed = True

    # --- 1. the reproduction must stop passing --------------------------------
    if reproduction_artifact:
        artifact = worktree / "test_whetstone_verify_repro.py"
        artifact.write_text(reproduction_artifact, encoding="utf-8")
        try:
            code, _ = _run(_select(test_command, artifact.name), worktree, image)
        finally:
            artifact.unlink(missing_ok=True)
        # PASSED means the defect is still reproducible, which means it is
        # still there. Anything else means the artifact no longer demonstrates
        # it -- which is what a fix looks like from this direction.
        outcome.reproduction_now_fails = code != _PASSED
        if not outcome.reproduction_now_fails:
            outcome.reasons.append(
                "the original reproduction still passes, so the defect is "
                "still present whatever the fix claims to have changed."
            )
    else:
        outcome.reasons.append(
            "there was no reproduction artifact to replay, so the fix could "
            "not be checked against the evidence that started this."
        )

    # --- 2. the regression test must fail without the fix ---------------------
    path = str(regression_test.get("path") or "")
    name = regression_test.get("test_name")
    command = _select(test_command, path, name)

    with_fix, _ = _run(command, worktree, image)
    outcome.regression_passes_with_the_fix = with_fix == _PASSED
    if with_fix in (_NOTHING_COLLECTED, _USAGE_ERROR):
        outcome.reasons.append(
            f"the regression test {path}::{name} collected nothing, so it does "
            f"not exist under that name (pytest exit {with_fix})."
        )
        return outcome
    if not outcome.regression_passes_with_the_fix:
        outcome.reasons.append(
            f"the regression test {path}::{name} fails against the fix it is "
            "supposed to be guarding."
        )
        return outcome

    # FORCE IT RED. Revert the fix, keep the test, and require the test to fail.
    # This is the check that this project has most often shipped without: a
    # regression test that passes against the unfixed code guards nothing, and
    # nothing about running it once can tell the difference.
    with _reverted(worktree, changed_files, keep=path):
        without_fix, _ = _run(command, worktree, image)
    outcome.regression_fails_without_the_fix = without_fix == _FAILED
    if not outcome.regression_fails_without_the_fix:
        outcome.reasons.append(
            f"the regression test {path}::{name} passes against the UNFIXED "
            f"code (exit {without_fix}), so it does not test the fix. A test "
            "that cannot fail is not a guard."
        )
        return outcome

    outcome.verified = bool(
        outcome.reproduction_now_fails
        and outcome.regression_fails_without_the_fix
        and outcome.regression_passes_with_the_fix
    )
    if outcome.verified:
        outcome.reasons.append(
            "verified: the original reproduction no longer passes, and the "
            "regression test fails without the fix and passes with it."
        )
    return outcome


class _reverted:
    """Put the worktree back to HEAD for the duration, except *keep*.

    Restores by copying the files aside rather than by `git stash`, which is
    forbidden in a shared tree and which would also take the regression test
    with it. `keep` is the test file: reverting it would make check 2 trivially
    pass by removing the thing being run.
    """

    def __init__(self, worktree: Path, changed: list[str], *, keep: str) -> None:
        self._worktree = worktree
        self._targets = [c for c in changed if c and c != keep]
        self._saved: dict[str, bytes | None] = {}

    def __enter__(self) -> None:
        for rel in self._targets:
            path = self._worktree / rel
            self._saved[rel] = path.read_bytes() if path.is_file() else None
            # `git checkout -- <path>` restores a tracked file to HEAD; an
            # untracked one has no HEAD version, so it is removed instead.
            result = subprocess.run(
                ["git", "--no-optional-locks", "checkout", "HEAD", "--", rel],
                cwd=self._worktree,
                capture_output=True,
                text=True,
                timeout=60,
                check=False,
            )
            if result.returncode != 0 and path.exists():
                path.unlink()

    def __exit__(self, *exc: object) -> None:
        for rel, content in self._saved.items():
            path = self._worktree / rel
            if content is None:
                path.unlink(missing_ok=True)
                continue
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)


__all__ = ["Verification", "verify"]
