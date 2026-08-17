"""A git worktree, which is the blast radius for anything that writes.

INVARIANT: the user's checkout is never written to. An implementer works in a
worktree created off a ref, and the checkout is measured before and after
rather than trusted -- the same stance `provider/sentinel.py` takes for
read-only stages, and for the same reason: a flag was wrong once and was
believed because a test asserted it reached the argv.

A worktree off a REF, not a copy of the working tree, so a dirty checkout is
irrelevant. People run tools on dirty trees; refusing would make the tool
unusable exactly when it is most wanted, and building from the ref means the
implementer starts from committed code rather than from somebody's half-done
edit.

REMOVAL IS GUARANTEED, including when the body raises. A leaked worktree is a
directory the next run trips over and a branch name that never becomes free
again -- and the body raising is the ordinary case here, not the exotic one,
because the thing running inside it is a model.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import warnings
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from .errors import GitError

# IMPORTED, NOT COPIED. The first version of this module reproduced sentinel's
# `-c` list and left the ENVIRONMENT inherited -- which is the same hole by
# another route: `cwd=root` does not stop `GIT_DIR` and `GIT_WORK_TREE` from
# repointing git at a different repository entirely, and `GIT_CONFIG_GLOBAL`
# reintroduces the very keys the `-c` overrides remove. Two copies of a
# security control drift; one does not.
from .provider.sentinel import _GIT_HARDENING as _HARDENING
from .provider.sentinel import _git_env

# Same ceiling `sentinel.py` uses. A git call that hangs is a run that hangs.
_TIMEOUT = 60


@lru_cache(maxsize=1)
def _empty_hooks_dir() -> Path:
    """A real, empty directory to point `core.hooksPath` at.

    NOT `/dev/null`. That is a POSIX device path, and on Windows git resolves
    it through an msys layer where its meaning is not guaranteed -- an empty
    value is worse still, because it restores `.git/hooks` and hands an
    inspected repository its hooks back. An existing empty directory is
    unambiguous everywhere and provably contains no hook.

    Measured on this machine: git 2.x on Windows did NOT create a relative
    `dev/null` from `-c core.hooksPath=/dev/null`, so the reported failure was
    not reproduced here. This is the defensive form rather than a proven fix,
    and it costs one `mkdir` per process.

    Cached: one directory per process, not one per git call.
    """
    path = Path(tempfile.mkdtemp(prefix="whetstone-nohooks-"))
    return path


def _hooks_override() -> list[str]:
    return ["-c", f"core.hooksPath={_empty_hooks_dir()}"]


def _git(root: Path, args: list[str], *, check: bool = True) -> str | None:
    """Run git in *root*, hardened, and return stdout.

    `run_argv`-style: a list, never a flattened string. Flattening means the
    host shell re-parses every quote inside, which broke differently on the
    Windows and Ubuntu CI legs -- the shape of every quoting defect is
    "correct on the machine it was written on".
    """
    try:
        completed = subprocess.run(
            ["git", "--no-optional-locks", *_HARDENING, *_hooks_override(),
             *args],
            cwd=root,
            env=_git_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_TIMEOUT,
        )
    except FileNotFoundError as exc:
        raise GitError("git is not on PATH, so no worktree can be created.") from exc
    except subprocess.TimeoutExpired as exc:
        raise GitError(f"git did not answer within {_TIMEOUT}s: git {' '.join(args)}") from exc
    if completed.returncode != 0:
        if check:
            raise GitError(
                f"git {' '.join(args)} failed in {root}: "
                f"{completed.stderr.strip() or completed.stdout.strip()}"
            )
        # None, not "". A best-effort call that FAILED and one that succeeded
        # with no output are different facts, and `_remove` reports the first.
        return None
    return completed.stdout.strip()


def _require_a_usable_repo(repo: Path, base_ref: str) -> None:
    """Refuse a repository nothing can be branched from, by name.

    Both refusals name the thing that is wrong. "fatal: not a valid object
    name" reaching a user through a traceback tells them nothing about which
    ref Whetstone asked for or why it was asking.
    """
    inside = _git(repo, ["rev-parse", "--is-inside-work-tree"], check=False)
    if inside != "true":
        raise GitError(
            f"{repo} is not a git repository (or not a work tree), so there is "
            "nothing to create a worktree from."
        )
    # An empty repository has a HEAD pointing at an unborn branch. `git
    # worktree add` fails there with a message about an invalid reference,
    # which reads as a typo in the ref rather than as "there are no commits".
    if not _git(repo, ["rev-parse", "--verify", "--quiet", "HEAD"], check=False):
        raise GitError(
            f"{repo} has no commit yet, so there is no ref to branch a worktree "
            "from. Commit something first."
        )
    if not _git(repo, ["rev-parse", "--verify", "--quiet", f"{base_ref}^{{commit}}"], check=False):
        raise GitError(
            f"{repo} has no ref named {base_ref!r} to branch from. Check "
            "`project.forge.base_branch` in whetstone.yaml."
        )


@contextmanager
def worktree(repo: Path, base_ref: str, *, run_id: str) -> Iterator[Path]:
    """A worktree off *base_ref*, removed when the block exits.

    The directory lives in the system temp area, NOT under *repo*. Inside the
    checkout, every write would land in the user's repository as an untracked
    directory, and `scope.resolver` would classify paths under it as project
    paths -- which is precisely the confusion the worktree exists to prevent.

    The branch name carries *run_id* so two runs against one project do not
    collide. One run per project is not a documented workflow, but two
    terminals are one keystroke apart -- the same argument `store.connect`'s
    busy timeout already makes.
    """
    repo = repo.resolve()
    _require_a_usable_repo(repo, base_ref)

    branch = f"whetstone/{run_id}"
    parent = Path(tempfile.mkdtemp(prefix=f"whetstone-{run_id}-"))
    tree = parent / "worktree"

    # `-B` rather than `-b`: a branch left behind by a previous crash would
    # otherwise make the same run id unusable forever, and the failure would
    # arrive as "a branch named X already exists" long after the crash that
    # caused it.
    # CREATION IS INSIDE CLEANUP'S OWNERSHIP. `parent` exists before the add
    # runs, so an add that fails -- a locked worktree, a full disk, a branch
    # checked out elsewhere -- used to leave the temp directory behind and
    # raise, with the `finally` never entered because it had not been reached.
    try:
        _git(repo, ["worktree", "add", "-B", branch, str(tree), base_ref])
    except BaseException:
        _remove(repo, tree, parent, branch)
        raise

    try:
        yield tree
    finally:
        _remove(repo, tree, parent, branch)


def _remove(repo: Path, tree: Path, parent: Path, branch: str) -> None:
    """Take the worktree down, whatever state it is in.

    `--force` because the whole point of the worktree is that something
    dirtied it, and `git worktree remove` refuses a dirty tree without it.
    Every step is best-effort IN SEQUENCE: a failure at one must not skip the
    next, or a git-level failure leaves the directory on disk as well.

    A CLEANUP FAILURE IS REPORTED, NOT SWALLOWED. An earlier version discarded
    every git and filesystem error here, so a locked worktree or a permission
    error left metadata, a branch, or a whole directory behind while the caller
    saw a clean return -- which is the "declined to do work and said nothing"
    shape this project refuses everywhere else. It still does not RAISE: the
    body's own exception, when there is one, is the one worth propagating, and
    a warning that replaces it would hide the actual failure.
    """
    left_behind: list[str] = []
    if _git(repo, ["worktree", "remove", "--force", str(tree)], check=False) is None:
        left_behind.append(f"worktree {tree}")

    # The temp parent is ours, so removing it is not a git operation and does
    # not depend on git having succeeded.
    try:
        shutil.rmtree(parent)
    except OSError as exc:
        left_behind.append(f"{parent} ({exc.strerror or exc})")

    # PRUNE AFTER the directory is gone, not before. `git worktree prune` only
    # clears a registration whose directory is missing -- so pruning first,
    # while the directory still existed, could not clear anything that
    # `remove --force` had just failed to. This ordering is the one that
    # recovers from that failure rather than compounding it.
    if _git(repo, ["worktree", "prune"], check=False) is None:
        left_behind.append("worktree metadata (prune failed)")

    # And the branch last: `branch -D` refuses a branch still checked out in a
    # registered worktree, so it can only succeed once the prune above has run.
    if _git(repo, ["branch", "-D", branch], check=False) is None:
        left_behind.append(f"branch {branch}")

    if left_behind:
        warnings.warn(
            "whetstone could not fully remove its worktree and left behind: "
            + ", ".join(left_behind)
            + ". Run `git worktree prune` in the repository to clear the "
            "metadata.",
            RuntimeWarning,
            stacklevel=2,
        )
