"""Pre-merge verification, exercised against a real container where it matters.

THE CHECK THIS MODULE EXISTS FOR is that a regression test fails without the
fix. This project has shipped the opposite more often than any other defect --
a symlink regression test that pre-created the leaf directory so both branches
matched, a severity test where the buggy ordering gave the same answer, an RCE
test that passed with the hardening removed. So the tests below are weighted
towards the ways `verified=True` could be produced by something that did not
happen.

The container-backed cases are gated on a real Linux daemon and SKIP WITH A
REASON where there is none. The Linux CI legs assert they must not skip.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from whetstone.sandbox import availability
from whetstone.verify import verify

IMAGE = "whetstone-e2e:latest"

_docker = pytest.mark.skipif(
    availability(IMAGE) is not None,
    reason=f"no linux-container docker daemon or {IMAGE} is absent",
)

# The reproduction PASSES while the defect is present. That direction is the
# convention, and getting it backwards inverts every assertion below.
_REPRO = (
    "from orders import average_price\n\n"
    "def test_reproduces():\n"
    "    try:\n"
    "        average_price([])\n"
    "    except ZeroDivisionError:\n"
    "        return\n"
    "    raise AssertionError('WHETSTONE-REPRO: no ZeroDivisionError')\n"
)

_BUGGY = (
    "def average_price(prices):\n"
    "    total = 0\n"
    "    for price in prices:\n"
    "        total += price\n"
    "    return total / len(prices)\n"
)

_FIXED = (
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


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


@pytest.fixture
def fixed_tree(tmp_path: Path) -> Path:
    """A worktree committed BUGGY, then fixed on top -- which is the real shape.

    The fix must be uncommitted, because verification reverts to HEAD to force
    the regression test red. Committing the fix would make HEAD already fixed
    and the revert a no-op, which is the quiet way this whole check becomes
    vacuous.
    """
    root = tmp_path / "tree"
    root.mkdir()
    _git(root, "init", "-b", "main", "-q")
    _git(root, "config", "user.email", "t@e.com")
    _git(root, "config", "user.name", "T")
    (root / "orders.py").write_text(_BUGGY, encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "buggy", "--no-gpg-sign", "-q")

    (root / "orders.py").write_text(_FIXED, encoding="utf-8")
    (root / "test_regression.py").write_text(_REGRESSION, encoding="utf-8")
    return root


def _verify(tree: Path, **overrides):
    kwargs = dict(
        reproduction_artifact=_REPRO,
        regression_test={"path": "test_regression.py", "test_name": "test_empty_is_zero"},
        changed_files=["orders.py", "test_regression.py"],
        test_command="python -m pytest -q",
        image=IMAGE,
    )
    kwargs.update(overrides)
    return verify(tree, **kwargs)


# --- refusals that need no container ---------------------------------------------


def test_no_sandbox_means_nothing_is_verified(tmp_path):
    """Not "verified by inspection". The next step after this is opening a pull
    request, so an unrunnable check must not read as a passing one."""
    outcome = _verify(tmp_path, image=None)
    assert outcome.verified is False
    assert outcome.executed is False
    assert any("not published" in r for r in outcome.reasons)


def test_no_regression_test_means_nothing_is_verified(tmp_path):
    """Checked BEFORE the sandbox, and the order matters.

    With the environment checked first, a user who had neither Docker nor a
    regression test was told to go install Docker -- which would not have
    helped, because there was nothing to run either way.
    """
    outcome = _verify(tmp_path, regression_test=None, image=None)
    assert outcome.verified is False
    assert any("no regression test" in r for r in outcome.reasons)


# --- the real thing ---------------------------------------------------------------


@_docker
def test_a_real_fix_verifies(fixed_tree):
    """All three sub-checks, in a container, against a real fix."""
    outcome = _verify(fixed_tree)
    assert outcome.executed is True
    assert outcome.reproduction_now_fails is True
    assert outcome.regression_passes_with_the_fix is True
    assert outcome.regression_fails_without_the_fix is True
    assert outcome.verified is True, outcome.reasons


@_docker
def test_a_regression_test_that_passes_without_the_fix_is_refused(fixed_tree):
    """THE CHECK THIS MODULE IS FOR.

    A test asserting something already true of the unfixed code guards nothing.
    Here it asserts a non-empty average, which the buggy version computes
    perfectly well -- so it passes both with and without the fix, and
    verification must refuse it.
    """
    (fixed_tree / "test_regression.py").write_text(
        "from orders import average_price\n\n"
        "def test_empty_is_zero():\n"
        "    assert average_price([2, 4]) == 3.0\n",
        encoding="utf-8",
    )
    outcome = _verify(fixed_tree)
    assert outcome.regression_passes_with_the_fix is True
    assert outcome.regression_fails_without_the_fix is False
    assert outcome.verified is False
    assert any("UNFIXED" in r for r in outcome.reasons)


@_docker
def test_a_perfect_regression_test_is_not_enough_without_the_reproduction(fixed_tree):
    """The reachable path where both regression checks pass and the answer is
    still no.

    With no artifact to replay, nothing has shown the ORIGINAL defect is gone
    -- only that a new test behaves as its author intended. Those are different
    claims, and `verified` requires the first as well as the second.

    This is the case that makes the three-way conjunction load-bearing rather
    than decorative: every other failure returns early, so a mutation reducing
    `verified` to the regression result alone survived until this existed.
    """
    outcome = _verify(fixed_tree, reproduction_artifact=None)
    assert outcome.regression_passes_with_the_fix is True
    assert outcome.regression_fails_without_the_fix is True
    assert outcome.reproduction_now_fails is None
    assert outcome.verified is False, outcome.reasons
    assert any("no reproduction artifact" in r for r in outcome.reasons)


@_docker
def test_a_fix_that_did_not_fix_anything_is_refused(fixed_tree):
    """The reproduction still passes, so the defect is still there -- whatever
    the implementer's summary said."""
    (fixed_tree / "orders.py").write_text(_BUGGY, encoding="utf-8")
    outcome = _verify(fixed_tree)
    assert outcome.reproduction_now_fails is False
    assert outcome.verified is False
    assert any("still passes" in r for r in outcome.reasons)


@_docker
def test_a_regression_test_that_does_not_exist_is_refused(fixed_tree):
    outcome = _verify(
        fixed_tree,
        regression_test={"path": "test_regression.py", "test_name": "test_typo"},
    )
    assert outcome.verified is False
    assert any("collected nothing" in r for r in outcome.reasons)


@_docker
def test_deleting_the_reproduction_does_not_buy_a_pass(fixed_tree):
    """The obvious attack: make the evidence pass by removing the evidence.

    The artifact is supplied by the CALLER from the stored finding and written
    fresh for each replay, so a fix that deleted it from the worktree changes
    nothing about what runs here.
    """
    (fixed_tree / "orders.py").write_text(_BUGGY, encoding="utf-8")
    outcome = _verify(fixed_tree, changed_files=["orders.py"])
    assert outcome.reproduction_now_fails is False, (
        "the replay used something other than the artifact it was handed"
    )


@_docker
def test_the_worktree_is_left_as_it_was_found(fixed_tree):
    """Verification reverts the fix to force the test red and must put it
    back. A verifier that leaves the tree reverted has silently discarded the
    change it just approved."""
    before = (fixed_tree / "orders.py").read_text(encoding="utf-8")
    _verify(fixed_tree)
    assert (fixed_tree / "orders.py").read_text(encoding="utf-8") == before
    assert (fixed_tree / "test_regression.py").exists()
    assert not (fixed_tree / "test_whetstone_verify_repro.py").exists()


@_docker
def test_an_untracked_fix_file_is_restored_too(fixed_tree):
    """`git checkout HEAD --` cannot restore a file HEAD never had, so the
    revert removes it -- and the restore has to put the content back from
    memory rather than from git."""
    (fixed_tree / "helper.py").write_text("VALUE = 1\n", encoding="utf-8")
    _verify(fixed_tree, changed_files=["orders.py", "helper.py", "test_regression.py"])
    assert (fixed_tree / "helper.py").read_text(encoding="utf-8") == "VALUE = 1\n"
