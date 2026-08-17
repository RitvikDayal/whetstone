"""M1b-2's definition of done: a fix written, verified, and never merged.

GREEN UNIT TESTS DO NOT SATISFY THIS. The chain below runs against a real git
repository with a real container: a worktree is created off a ref, a fix is
written into it, the fix is verified by replaying the finding's own evidence,
and routing decides whether anything may be published.

WHAT IS STUBBED AND WHY, stated rather than buried. The IMPLEMENTER is a fake
provider writing a known-good fix. That is deliberate: this test is about the
machinery around a fix -- worktree isolation, verification, routing, refusal --
and a real model call would make every assertion below depend on what a model
happened to produce that minute. `test_implement.py` covers the stage's own
contract, and the M1a measurement covers a real model in the loop.

WHAT IS NOT STUBBED: the worktree, the container, the verification, the git
operations, and the checkout that must come back untouched.

THE `gh` BOUNDARY IS THE ONLY MOCK IN THE PUBLICATION PATH. Nothing here opens
a real pull request against a real repository -- D20 says read-and-report only
for public repos, and opening one against a scratch repo would leave litter
somebody has to clean up. The argv is asserted instead, which is what decides
whether a PR would be a draft.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from conftest import needs_docker
from whetstone.lenses.code_defects.implement import implement
from whetstone.provider.base import StageResult, Usage
from whetstone.queue.routing import Action, action_for, as_draft
from whetstone.sinks import github
from whetstone.verify import verify
from whetstone.worktree import worktree

_BUGGY = (
    "def average_price(prices):\n"
    "    total = 0\n"
    "    for price in prices:\n"
    "        total += price\n"
    "    return total / len(prices)\n"
)

_FIX = (
    "def average_price(prices):\n"
    "    if not prices:\n"
    "        return 0.0\n"
    "    total = 0\n"
    "    for price in prices:\n"
    "        total += price\n"
    "    return total / len(prices)\n"
)

_REGRESSION = (
    "from orders import average_price\n\n"
    "def test_empty_is_zero():\n"
    "    assert average_price([]) == 0.0\n"
)

# PASSES while the defect is present. That direction is the convention.
_REPRO = (
    "from orders import average_price\n\n"
    "def test_reproduces():\n"
    "    try:\n"
    "        average_price([])\n"
    "    except ZeroDivisionError:\n"
    "        return\n"
    "    raise AssertionError('WHETSTONE-REPRO: no ZeroDivisionError')\n"
)

_CANDIDATE = {
    "subject": "orders.py:5",
    "title": "division by zero on an empty list",
    "observation": "average_price divides by len(prices) with no guard.",
    "root_cause_hypothesis": "No empty check before the division.",
    "failure_scenario": "average_price([]) raises ZeroDivisionError.",
    "severity": "medium",
}
_REPRODUCTION = {
    "executed": True,
    "verdict": "reproduced",
    "reproduced": True,
    "has_runnable_artifact": True,
    "artifact": {"kind": "pytest", "content": _REPRO},
}
_VERDICT = {
    "confirmed": True,
    "challenged": True,
    "strongest_counterargument": "Callers may guarantee a non-empty list.",
}


def _git(root: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=root, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def checkout(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    root.mkdir()
    _git(root, "init", "-b", "main", "-q")
    _git(root, "config", "user.email", "t@e.com")
    _git(root, "config", "user.name", "T")
    (root / "orders.py").write_text(_BUGGY, encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "buggy", "--no-gpg-sign", "-q")
    return root


class _WritesTheFix:
    """A provider that really writes into the worktree it is given.

    Not a mock returning a payload: it performs the writes, so the sentinel,
    `changed_files` and the verifier all see a real change on disk. What it does
    not do is think.
    """

    name = "writes-the-fix"

    def __init__(self, tree: Path, *, sneaky: bool = False) -> None:
        self._tree = tree
        self._sneaky = sneaky
        self.requests: list = []

    def run_stage(self, request):
        self.requests.append(request)
        (self._tree / "orders.py").write_text(_FIX, encoding="utf-8")
        (self._tree / "test_regression.py").write_text(_REGRESSION, encoding="utf-8")
        if self._sneaky:
            (self._tree / "unreported.py").write_text("x = 1\n", encoding="utf-8")
        return StageResult(
            ok=True,
            data={
                "changed_files": ["orders.py", "test_regression.py"],
                "summary": "Return 0.0 for an empty list.",
                "regression_test": {
                    "path": "test_regression.py",
                    "test_name": "test_empty_is_zero",
                },
                "notes": None,
            },
            raw="{}",
            usage=Usage(cost_usd=0.01, input_tokens=10),
            error=None,
            turns=3,
            denials=(),
            mutation="the worktree changed while the stage ran: M orders.py",
        )


def _ctx(root: Path):
    from whetstone.lenses.base import RunContext

    return RunContext(
        project_root=root,
        state_root=root / ".state",
        files=(),
        tier="deep",
        lens_options={},
        run_id="r1",
    )


# --- the whole chain ----------------------------------------------------------------


@needs_docker
def test_a_fix_is_written_verified_and_the_checkout_is_untouched(
    checkout, sandbox_image
):
    """M1b-2's definition of done, end to end."""
    before = (checkout / "orders.py").read_text(encoding="utf-8")
    head_before = _git(checkout, "rev-parse", "HEAD")

    with worktree(checkout, "main", run_id="e2e1") as tree:
        provider = _WritesTheFix(tree)
        outcome, skips = implement(
            _CANDIDATE, _REPRODUCTION, _VERDICT, tree, _ctx(checkout), provider
        )
        assert outcome["implemented"] is True, skips
        assert "orders.py" in outcome["changed_files"]

        result = verify(
            tree,
            reproduction_artifact=_REPRO,
            regression_test=outcome["regression_test"],
            changed_files=outcome["changed_files"],
            test_command="python -m pytest -q",
            image=sandbox_image,
        )
        assert result.verified is True, result.reasons
        assert result.reproduction_now_fails is True
        assert result.regression_fails_without_the_fix is True

    # THE POINT OF ALL OF IT: nothing reached the user's checkout.
    assert (checkout / "orders.py").read_text(encoding="utf-8") == before
    assert not (checkout / "test_regression.py").exists()
    assert _git(checkout, "rev-parse", "HEAD") == head_before
    assert _git(checkout, "status", "--porcelain") == ""


@needs_docker
def test_a_fix_that_does_not_fix_it_is_not_verified(checkout, sandbox_image):
    """The counterweight. If verification passes a broken fix, everything above
    it is decoration."""
    with worktree(checkout, "main", run_id="e2e2") as tree:
        # A "fix" that changes nothing about the defect.
        (tree / "orders.py").write_text(_BUGGY + "\n# a comment\n", encoding="utf-8")
        (tree / "test_regression.py").write_text(_REGRESSION, encoding="utf-8")

        result = verify(
            tree,
            reproduction_artifact=_REPRO,
            regression_test={
                "path": "test_regression.py",
                "test_name": "test_empty_is_zero",
            },
            changed_files=["orders.py", "test_regression.py"],
            test_command="python -m pytest -q",
            image=sandbox_image,
        )
        assert result.verified is False
        assert result.reasons


@needs_docker
def test_a_stage_that_touched_more_than_it_reported_is_recorded(
    checkout, sandbox_image
):
    """`changed_files` comes from git, and the disagreement is reported rather
    than resolved in the model's favour."""
    with worktree(checkout, "main", run_id="e2e3") as tree:
        provider = _WritesTheFix(tree, sneaky=True)
        outcome, skips = implement(
            _CANDIDATE, _REPRODUCTION, _VERDICT, tree, _ctx(checkout), provider
        )
        assert "unreported.py" in outcome["changed_files"]
        assert "unreported.py" not in outcome["claimed_files"]
        assert any("did not report" in s for s in skips)


# --- publication is decided by what was earned, and nothing merges ------------------


@needs_docker
def test_a_verified_fix_from_an_unearned_lens_is_not_published(
    checkout, sandbox_image, tmp_path, monkeypatch
):
    """Verified is necessary and not sufficient. A lens on probation gets its
    fix left on a branch with the branch NAMED, not published."""
    from whetstone.store.db import connect

    conn = connect(tmp_path / "state")
    action, _ = action_for(conn, "code-defects", configured_ceiling=3, trust=None)
    conn.close()
    assert action is Action.propose

    recorded: list[list[str]] = []
    monkeypatch.setattr(
        github, "_run", lambda argv, cwd=None: (recorded.append(argv), (0, "u", ""))[1]
    )
    sink = github.GitHubPullRequest(repo="o/r", project_root=checkout)
    publication = sink.publish(
        _StubFinding(), branch="whetstone/e2e", verified=True, level=1
    )
    assert publication.published is False
    assert "whetstone/e2e" in publication.reason
    assert recorded == [], "gh was invoked for a lens that had not earned it"


@needs_docker
def test_a_verified_fix_from_an_earned_lens_opens_a_draft_never_a_merge(
    checkout, monkeypatch
):
    recorded: list[list[str]] = []

    def _fake(argv, cwd=None):
        recorded.append(argv)
        return 0, "https://github.com/o/r/pull/1", ""

    monkeypatch.setattr(github, "_run", _fake)
    sink = github.GitHubPullRequest(repo="o/r", project_root=checkout)
    publication = sink.publish(
        _StubFinding(), branch="whetstone/e2e", verified=True, level=2
    )
    assert publication.published is True
    argv = recorded[0]
    assert argv[:3] == ["gh", "pr", "create"]
    assert "--draft" in argv
    assert as_draft(Action.draft_pr) is True
    # The invariant, at the last place it could be broken -- checked on the
    # COMMAND and the FLAGS, not the body. The body says "it never merges and
    # never deploys", so scanning every argv element catches the sentence that
    # promises the thing rather than the thing.
    flags = [p for p in argv if p.startswith("--")] + argv[:3]
    assert not any("merge" in f or "deploy" in f for f in flags), flags


class _StubFinding:
    id = "abc"
    title = "division by zero on an empty list"
    subject = "orders.py:5"
    lens = "code-defects"
    severity = "medium"
    detail = "d"
    grade = "A"
    grade_reason = "graded A: reproduced."
    evidence = {"data": {"reproduction": {"verdict": "reproduced"}}}
