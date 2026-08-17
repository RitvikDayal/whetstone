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
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from .errors import GitError

# Same ceiling `sentinel.py` uses. A git call that hangs is a run that hangs.
_TIMEOUT = 60

# Config keys that make git execute something on an ordinary command, forced
# off for every call here. `core.fsmonitor` from an inspected repository's own
# .git/config was arbitrary code execution from `git status` -- found in the
# M1a gate, before any model ran and with zero tokens spent. This module runs
# git against a repository Whetstone did not write, so it inherits that.
_HARDENING = [
    "-c", "core.fsmonitor=",
    "-c", "core.hooksPath=/dev/null",
    "-c", "protocol.ext.allow=never",
    "-c", "core.sshCommand=",
    "-c", "diff.external=",
    "-c", "core.pager=cat",
    "-c", "sequence.editor=",
    "-c", "core.editor=",
]


def _git(root: Path, args: list[str], *, check: bool = True) -> str:
    """Run git in *root*, hardened, and return stdout.

    `run_argv`-style: a list, never a flattened string. Flattening means the
    host shell re-parses every quote inside, which broke differently on the
    Windows and Ubuntu CI legs -- the shape of every quoting defect is
    "correct on the machine it was written on".
    """
    try:
        completed = subprocess.run(
            ["git", "--no-optional-locks", *_HARDENING, *args],
            cwd=root,
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
    if check and completed.returncode != 0:
        raise GitError(
            f"git {' '.join(args)} failed in {root}: "
            f"{completed.stderr.strip() or completed.stdout.strip()}"
        )
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
    _git(repo, ["worktree", "add", "-B", branch, str(tree), base_ref])
    try:
        yield tree
    finally:
        _remove(repo, tree, parent, branch)


def _remove(repo: Path, tree: Path, parent: Path, branch: str) -> None:
    """Take the worktree down, whatever state it is in.

    `--force` because the whole point of the worktree is that something
    dirtied it, and `git worktree remove` refuses a dirty tree without it.
    Every step is best-effort in sequence: a failure at one must not skip the
    next, or a git-level failure leaves the directory on disk as well.
    """
    _git(repo, ["worktree", "remove", "--force", str(tree)], check=False)
    _git(repo, ["worktree", "prune"], check=False)
    _git(repo, ["branch", "-D", branch], check=False)
    # The temp parent is ours, so removing it is not a git operation and does
    # not depend on git having succeeded.
    shutil.rmtree(parent, ignore_errors=True)
