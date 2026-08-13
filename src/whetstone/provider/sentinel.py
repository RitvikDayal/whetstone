"""Did the worktree change while a stage ran?

WHY THIS EXISTS, stated for whoever wants to delete it as redundant with the
policy gate. The gate's first version mapped `PermissionSet` onto
`--allowedTools`, which auto-approves rather than bounds, so every stage held
the CLI's full default toolset. The tests were green. They asserted the flag
appeared in the argv, and it did; what nobody asserted was the effect. A
reviewer running the real binary got a read-only stage to create files, append
to README.md and run `git init`.

So the guarantee no longer rests on the CLI honouring a flag. This module reads
the filesystem after the fact and cannot be fooled by a flag whose meaning we
misread, or whose meaning changes in a later release. Every M1a stage is
read-only, so ANY mutation is a defect and gets surfaced rather than repaired.

CONTENT, NOT STATUS LETTERS. The first version recorded `git status --porcelain`
output and nothing else, and a reviewer proved it missed seven of eight mutation
classes -- including **appending to an already-dirty tracked file**, which is
the original attack verbatim. ` M README.md` reads identically before and after,
and a working tree is normally dirty mid-run. Every path git lists is now
hashed, so the letter and the bytes both have to match.

WHAT IT DOES NOT SEE, so nobody mistakes it for total. This list is the honest
one; the first version disclosed two items and missed the five that mattered.

- **A write followed by a delete.** Undetectable by any before/after comparison,
  by construction. Not a gap that can be closed here.
- **Objects and the index inside `.git/`.** The execution surface is covered --
  `config`, `hooks/`, and every ref -- because those are what turn a repository
  into a program. A loose object nothing references is inert, and hashing every
  object would cost more than the stage.
- **Ignored files, by content.** They are listed and stat-ed, so an appearance
  or a size change is caught; a same-size same-mtime rewrite inside ignored
  space is not. Hashing `node_modules` per stage is not worth it.
- **Reads.** A stage that exfiltrates is not a mutation and this says nothing
  about it. `read_denied` is the nominal control and reaches no CLI flag, so
  there is currently NO control -- see `policy/profiles.py`.
- **Anything outside *root*.**

AND IT RUNS GIT AGAINST A REPOSITORY IT DOES NOT TRUST. `core.fsmonitor`,
`core.pager`, `core.hooksPath` and `core.sshCommand` all name programs git
executes, and git reads them from the inspected worktree's own `.git/config`. A
reviewer planted one and got arbitrary code execution out of `fingerprint()` --
before any model ran, with zero tokens spent. `_GIT_HARDENING` below neutralises
the ones reachable from `status` and `for-each-ref`. **It is a deny-list and is
therefore incomplete**; the durable answer is a process boundary, which is not
M1a's.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
from pathlib import Path

# A non-repo root is walked instead, and a walk of an arbitrary directory is
# unbounded. Stopping is better than hanging a stage; the stop is recorded in
# the fingerprint itself so it is never a silent partial answer.
_WALK_CAP = 20_000

# Content hashing has to stop somewhere. A file over this is recorded by size
# and mtime alone, and says so in the fingerprint rather than looking hashed.
_HASH_CAP_BYTES = 8 * 1024 * 1024

_GIT_TIMEOUT = 60

# Config that makes `git` run somebody else's program, forced off. Passed with
# `-c` so it overrides the inspected repository's own `.git/config`, which is
# the file an attacker controls.
#
# `core.quotepath=false` is here for a second reason: git escapes non-ASCII
# paths to ASCII octal by default, so `cafe-<accented>.txt` reaches the user as
# `"caf\303\251.txt"` -- and the `errors="surrogateescape"` on this call is
# then defending against a hazard it never sees.
_GIT_HARDENING = [
    "-c", "core.fsmonitor=",
    "-c", "core.hooksPath=/dev/null",
    "-c", "core.pager=cat",
    "-c", "core.sshCommand=",
    "-c", "core.editor=",
    "-c", "diff.external=",
    "-c", "core.alternateRefsCommand=",
    "-c", "protocol.ext.allow=never",
    "-c", "core.quotepath=false",
]

# The parts of `.git/` that decide whether the repository is also a program.
_GIT_EXECUTION_SURFACE = ("config", "hooks")


def _git(root: Path, args: list[str]) -> str | None:
    """git's stdout, or None if git could not answer for any reason.

    None is a real answer here rather than an error: this runs on every stage
    and a sentinel that takes the run down when git is missing is worse than no
    sentinel. The caller degrades to a walk AND records that it did.
    """
    try:
        proc = subprocess.run(
            # `--no-optional-locks` stops `git status` refreshing `.git/index`,
            # which would otherwise make the index differ between the before and
            # after fingerprints on every single stage.
            ["git", "--no-optional-locks", *_GIT_HARDENING, *args],
            cwd=root,
            capture_output=True,
            # git writes path bytes as it stored them. `text=True` alone decodes
            # with the locale codec, which under cp1252 mangles every accented
            # filename; see `scope/resolver.py`, where that shipped.
            encoding="utf-8",
            errors="surrogateescape",
            timeout=_GIT_TIMEOUT,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return proc.stdout if proc.returncode == 0 else None


def _stat_mark(path: Path) -> str:
    """Size and mtime, or an honest absence. Never raises."""
    try:
        stat = path.stat()
    except OSError:
        return "absent"
    return f"{stat.st_size}:{stat.st_mtime_ns}"


def _digest(path: Path) -> str:
    """A content mark for one path: its hash, or an honest reason there is none.

    Never raises. This runs over paths a stage may have just deleted or made
    unreadable, and each of those states is itself a change worth recording
    rather than an error worth propagating.
    """
    try:
        stat = path.stat()
    except OSError:
        return "absent"
    if not path.is_file():
        return f"nonfile:{stat.st_mode:o}"
    if stat.st_size > _HASH_CAP_BYTES:
        # Says `stat` rather than looking like a hash. A mark that silently
        # degrades is the shape this module exists to avoid.
        return f"stat {_stat_mark(path)}"
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError:
        return "unreadable"
    return f"sha256:{digest.hexdigest()[:32]}"


def _status_paths(status: str) -> list[tuple[str, str]]:
    """`(status letters, path)` for each porcelain v1 line.

    Rename and copy entries carry `old -> new`; the new path is the one whose
    content matters, and the whole raw line is kept in the fingerprint anyway,
    so the rename itself is not lost.
    """
    out = []
    for line in status.splitlines():
        if len(line) < 4:
            continue
        letters, path = line[:2], line[3:]
        if " -> " in path:
            path = path.split(" -> ", 1)[1]
        out.append((letters, path.strip('"')))
    return out


def _git_surface(root: Path) -> list[str]:
    """The parts of `.git/` that decide whether the repository is a program.

    `git status` never reports anything under `.git/`, so writing
    `.git/hooks/post-checkout` or an aliased `.git/config` was completely
    invisible to the first version -- while `.git/config` is simultaneously the
    file that makes `git` execute an arbitrary program. Both halves of that are
    proven.
    """
    toplevel = _git(root, ["rev-parse", "--show-toplevel"])
    git_dir = (Path(toplevel.strip()) if toplevel and toplevel.strip() else root) / ".git"
    if not git_dir.is_dir():
        return []
    entries = []
    for name in _GIT_EXECUTION_SURFACE:
        target = git_dir / name
        if target.is_dir():
            for child in sorted(target.rglob("*")):
                if child.is_file():
                    rel = child.relative_to(git_dir).as_posix()
                    entries.append(f".git/{rel} {_digest(child)}")
        elif target.exists():
            entries.append(f".git/{name} {_digest(target)}")
    refs = _git(root, ["for-each-ref", "--format=%(refname) %(objectname)"])
    if refs is not None:
        entries.extend(f"ref {line}" for line in sorted(refs.splitlines()) if line)
    return entries


def _walk(root: Path) -> list[str]:
    """Every file under *root* as `relpath size:mtime_ns`, sorted, then capped.

    SORTED GLOBALLY, not per-directory. `os.walk` is depth-first, so sorting
    inside the loop gives a deterministic order that is not a lexicographic one
    -- and the cap then decides membership by traversal position. Adding one
    file could change WHICH entries survive truncation and report a large
    spurious mutation. Sorting first makes the surviving set the
    lexicographically-first `_WALK_CAP`, which changes only when those entries
    change.

    That is also why the sort has to happen here rather than being left to the
    filesystem: NTFS returns directory entries in name order and ext4 returns
    them in hash order, so an unsorted walk is accidentally fine on the Windows
    legs and wrong on the Ubuntu ones.

    The TOTAL count is recorded whenever the cap bites. Without it, a file
    added past the cap is invisible -- a silent hole in exactly the case the
    cap exists to bound.
    """
    entries: list[str] = []
    for parent, dirs, names in os.walk(root, onerror=entries.append):
        dirs.sort()
        for name in names:
            path = Path(parent) / name
            try:
                stat = path.stat()
                mark = f"{stat.st_size}:{stat.st_mtime_ns}"
            except OSError:
                # Unreadable is itself a state, and a file that becomes
                # unreadable during a stage is a change worth reporting.
                mark = "unreadable"
            entries.append(f"{path.relative_to(root).as_posix()} {mark}")
    entries = sorted(str(entry) for entry in entries)
    if len(entries) > _WALK_CAP:
        return [f"... {len(entries)} entries, first {_WALK_CAP} shown"] + entries[
            :_WALK_CAP
        ]
    return entries


def fingerprint(root: Path) -> str:
    """A canonical description of *root*'s working state.

    Returned as the text rather than as a digest, which is a deliberate
    departure from the plan's "hashed": a digest compares in one line and then
    cannot say WHAT changed, and a sentinel that reports "something happened"
    sends the reader to look for it by hand. Comparison is still exact -- two
    of these are equal or they are not.

    The first line names the mode, so a run that silently degraded from git to
    a walk differs from one that did not, instead of matching it.
    """
    # Status first, and HEAD only if it answered. This runs twice per stage, so
    # a non-repo root would otherwise pay for two failed git spawns before
    # falling back to the walk it was always going to use.
    #
    # `-- .` is load-bearing: without it `git status` reports the WHOLE
    # repository regardless of cwd, so watching `repo/sub` reported a file
    # created at `repo/outside.txt`. The two fingerprint modes would then have
    # different scopes -- the one property a mode-switching check must not
    # have -- and any unrelated edit elsewhere in the user's repo would fail the
    # stage as a read-only violation.
    #
    # `--ignored` is affordable now and was not before: with no stage holding a
    # shell, nothing churns `.pytest_cache` or a build directory, so listing
    # ignored paths costs a listing and buys the `.gitignore: *` case, where an
    # attacker-supplied ignore file otherwise hides every write.
    status = _git(
        root,
        ["status", "--porcelain=v1", "--untracked-files=all", "--ignored", "--", "."],
    )
    if status is None:
        return "\n".join(["mode git-unavailable", *_walk(root)])
    head = _git(root, ["rev-parse", "HEAD"])

    # Status paths are relative to the REPOSITORY ROOT, never to cwd. Joining
    # them onto `root` worked only when the two were the same directory, and
    # silently produced `absent` for every path when watching a subdirectory --
    # so content hashing was off exactly where the scoping fix put it. This is
    # the same defect `scope/resolver.py` records under `--full-name`.
    toplevel = _git(root, ["rev-parse", "--show-toplevel"])
    base = Path(toplevel.strip()) if toplevel and toplevel.strip() else root

    lines = []
    for letters, relative in _status_paths(status):
        target = base / relative
        # Ignored paths are listed and stat-ed, never hashed: hashing
        # `node_modules` on every stage is not worth what it buys, and the
        # disclosure list at the top says so plainly rather than leaving the
        # reader to infer it from a mark that looks like every other mark.
        mark = f"ignored {_stat_mark(target)}" if letters == "!!" else _digest(target)
        lines.append(f"{letters} {relative} {mark}")

    # An unborn branch has no HEAD and that is a legitimate state, not a
    # failure; it is recorded as itself so a first commit during a stage shows
    # up as a change.
    return "\n".join(
        [
            "mode git",
            f"head {(head or 'unborn').strip()}",
            *sorted(lines),
            *_git_surface(root),
        ]
    )


def assert_unchanged(root: Path, before: str) -> str | None:
    """What changed since *before*, or None.

    The return is a sentence for a human, because the caller's job is to put it
    in front of one. A read-only stage that modified the repository is the kind
    of thing that has to be read, not counted.
    """
    after = fingerprint(root)
    if after == before:
        return None

    was = set(before.splitlines())
    now = set(after.splitlines())
    added = sorted(now - was)
    removed = sorted(was - now)
    parts = []
    if added:
        parts.append("now: " + "; ".join(added[:5]))
        if len(added) > 5:
            parts.append(f"(+{len(added) - 5} more)")
    if removed:
        parts.append("was: " + "; ".join(removed[:5]))
        if len(removed) > 5:
            parts.append(f"(+{len(removed) - 5} more)")
    return (
        f"the worktree at {root} changed while the stage ran, and every M1a "
        f"stage is read-only. " + " ".join(parts)
    )
