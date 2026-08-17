"""The worktree is the blast radius, and these tests are about where writes
did NOT go.

Invariant 2 of M1b-2: every write lands inside the worktree, and the worktree
is not the user's checkout. Asserted by measuring the checkout, not by
trusting a flag -- the same stance `sentinel.py` already takes for read-only
stages, and for the same reason: the flags were wrong once and were believed
because a test asserted they appeared in the argv.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from whetstone.errors import GitError
from whetstone.worktree import worktree


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "checkout"
    root.mkdir()
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "t@example.com")
    _git(root, "config", "user.name", "Test")
    (root / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-m", "init", "--no-gpg-sign")
    return root


def _tracked_state(root: Path) -> tuple[str, str]:
    """HEAD plus porcelain status: what the user would notice."""
    return _git(root, "rev-parse", "HEAD"), _git(root, "status", "--porcelain")


# --- the checkout is never written to -------------------------------------------


def test_a_write_in_the_worktree_does_not_reach_the_checkout(repo):
    before = _tracked_state(repo)
    original = (repo / "app.py").read_text(encoding="utf-8")

    with worktree(repo, "main", run_id="r1") as tree:
        assert tree != repo
        (tree / "app.py").write_text("BROKEN\n", encoding="utf-8")
        (tree / "new_file.py").write_text("x = 1\n", encoding="utf-8")
        assert (tree / "app.py").read_text(encoding="utf-8") == "BROKEN\n"

    assert (repo / "app.py").read_text(encoding="utf-8") == original
    assert not (repo / "new_file.py").exists()
    assert _tracked_state(repo) == before


def test_the_worktree_starts_from_the_named_ref(repo):
    with worktree(repo, "main", run_id="r1") as tree:
        assert (tree / "app.py").read_text(encoding="utf-8").startswith("def add")


# --- removal is guaranteed --------------------------------------------------------


def test_the_worktree_is_removed_on_the_happy_path(repo):
    with worktree(repo, "main", run_id="r1") as tree:
        path = tree
    assert not path.exists()
    assert "r1" not in _git(repo, "worktree", "list")


def test_the_worktree_is_removed_when_the_body_raises(repo):
    """A leaked worktree is a directory the next run trips over, and a branch
    name that never becomes free again."""
    with pytest.raises(RuntimeError), worktree(repo, "main", run_id="r1") as tree:
        path = tree
        raise RuntimeError("the implementer blew up")
    assert not path.exists()
    assert "r1" not in _git(repo, "worktree", "list")


def test_a_dirty_worktree_is_still_removed(repo):
    """`git worktree remove` refuses a dirty tree without --force, and the
    whole point of this thing is that something dirtied it.

    Asserted on GIT's bookkeeping as well as the filesystem. The temp parent is
    removed with `shutil.rmtree` whatever git did, so `not path.exists()` holds
    even when `remove` refused -- and the stale registration then sits in the
    user's `git worktree list` until something else prunes it. Dropping
    `--force` survived a battery that checked only that the directory was gone.
    """
    with worktree(repo, "main", run_id="r1") as tree:
        path = tree
        (tree / "app.py").write_text("modified\n", encoding="utf-8")
        (tree / "untracked.txt").write_text("stray\n", encoding="utf-8")
    assert not path.exists()
    assert "r1" not in _git(repo, "worktree", "list"), (
        "git still lists the worktree, so the user's repository is left "
        "carrying a stale registration"
    )


def test_the_branch_is_removed_too(repo):
    """A left-behind branch makes the same run id unusable, and run ids repeat
    across projects far more readily than uuids do."""
    with worktree(repo, "main", run_id="r1"):
        pass
    branches = _git(repo, "branch", "--list")
    assert "r1" not in branches


# --- refusals ----------------------------------------------------------------------


def test_a_repository_with_no_commits_is_refused_with_a_reason(tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    _git(root, "init", "-b", "main")
    with pytest.raises(GitError, match="no commit"), worktree(root, "main", run_id="r1"):
        pass


def test_a_directory_that_is_not_a_repository_is_refused(tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(GitError), worktree(plain, "main", run_id="r1"):
        pass


def test_an_unknown_ref_is_refused_with_something_actionable(repo):
    """Asserting only that the message contains the ref name proves nothing:
    git's own "invalid reference: nonesuch" contains it too, so the test
    passed with the pre-check deleted. What the pre-check buys is naming the
    CONFIG KEY the ref came from, which git cannot know."""
    with pytest.raises(GitError, match="base_branch"), worktree(repo, "nonesuch", run_id="r1"):
        pass


# --- the checkout being dirty is not a blocker --------------------------------------


def test_a_dirty_checkout_does_not_stop_a_worktree(repo):
    """People run tools on dirty trees. A worktree off a ref does not care, and
    refusing would make the tool unusable exactly when it is most wanted."""
    (repo / "app.py").write_text("work in progress\n", encoding="utf-8")
    (repo / "scratch.txt").write_text("notes\n", encoding="utf-8")

    with worktree(repo, "main", run_id="r1") as tree:
        # The worktree is built from the REF, so it does not carry the dirt.
        assert (tree / "app.py").read_text(encoding="utf-8").startswith("def add")
        assert not (tree / "scratch.txt").exists()

    assert (repo / "app.py").read_text(encoding="utf-8") == "work in progress\n"
    assert (repo / "scratch.txt").exists()


# --- concurrency --------------------------------------------------------------------


def test_two_worktrees_at_once_do_not_collide(repo):
    """One run per project is not a documented workflow, but two terminals are
    one keystroke apart -- the same argument `store.connect`'s busy timeout
    already makes."""
    with (
        worktree(repo, "main", run_id="r1") as first,
        worktree(repo, "main", run_id="r2") as second,
    ):
        assert first != second
        (first / "a.py").write_text("1\n", encoding="utf-8")
        (second / "a.py").write_text("2\n", encoding="utf-8")
        assert (first / "a.py").read_text(encoding="utf-8") == "1\n"
        assert (second / "a.py").read_text(encoding="utf-8") == "2\n"
    assert not first.exists()
    assert not second.exists()


def test_the_same_run_id_twice_in_a_row_works(repo):
    """The first call must leave nothing behind for the second to trip on."""
    with worktree(repo, "main", run_id="r1") as first:
        first_path = first
    with worktree(repo, "main", run_id="r1") as second:
        assert second.exists()
    assert not first_path.exists()
    assert not second.exists()


# --- the worktree is not inside the checkout -----------------------------------------


def test_the_worktree_lives_outside_the_checkout(repo):
    """Inside it, every write would land in the user's repository as an
    untracked directory -- and `scope.resolver` would classify paths under it
    as project paths, which is exactly the confusion this exists to prevent."""
    with worktree(repo, "main", run_id="r1") as tree:
        assert repo.resolve() not in tree.resolve().parents
        assert tree.resolve() != repo.resolve()
