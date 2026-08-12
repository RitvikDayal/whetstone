"""Resolve boundaries + git state into the files a run may examine.

`include` and `exclude` decide what is *analysed*. `never_touch` decides what may
be *written*, and deliberately does not filter analysis -- a finding in a
protected path is still worth telling a human about.

Path contract, because two different frames are in play. Git is queried in
REPO-ROOT-relative terms, since that is the only frame `git diff --name-only`
speaks and the two sets have to be intersected. Everything this module RETURNS
is relative to the PROJECT root: that is the frame `include`/`exclude` patterns
in whetstone.yaml are written against, and the frame `project_root / path`
needs. `find_config` supports a config at or below the worktree root, so the two
frames differ for any monorepo and the conversion is not optional.
"""

from __future__ import annotations

import contextlib
import errno
import os
import re
import stat
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import IO, NoReturn

import pathspec

from ..config.model import BoundariesConfig
from ..errors import GitError, WriteForbiddenError

# A lone surrogate is what `surrogateescape` leaves behind for a byte that was
# not valid UTF-8. Such a path cannot be opened, encoded, or stored.
_LONE_SURROGATE = re.compile("[\udc80-\udcff]")


def _spec(patterns: list[str]) -> pathspec.PathSpec:
    return pathspec.PathSpec.from_lines("gitwildmatch", patterns)


def _git(project_root: Path, args: list[str]) -> str:
    # encoding is explicit: git writes path bytes exactly as it stored them,
    # which is UTF-8, while `text=True` alone decodes with the locale codec.
    # Under cp1252 that turned every accented filename into a path that does not
    # exist (silently dropped from the run) and killed the reader thread outright
    # on a decomposed one, leaving stdout None and returncode 0 -- a bare
    # AttributeError two frames later. errors="surrogateescape" keeps genuinely
    # non-UTF-8 names (possible on Linux) decodable; resolve_files reports them
    # rather than passing surrogates downstream.
    proc = subprocess.run(
        ["git", *args],
        cwd=project_root,
        capture_output=True,
        encoding="utf-8",
        errors="surrogateescape",
    )
    if proc.returncode != 0:
        raise GitError(
            f"`git {' '.join(args)}` failed in {project_root}: "
            f"{proc.stderr.strip() or 'no stderr'}"
        )
    return proc.stdout


def _git_prefix(project_root: Path) -> str:
    """Where *project_root* sits inside the worktree: "" or "apps/web/"."""
    return _git(project_root, ["rev-parse", "--show-prefix"]).rstrip("\r\n")


def git_tracked(project_root: Path) -> list[Path]:
    """Tracked files at or below *project_root*, relative to the REPO root.

    `--full-name` is the whole point: without it git prints paths relative to
    the cwd, and intersecting those with `git diff --name-only` -- which is
    always repo-root relative -- produced an empty set for every project root
    below the repo root.
    """
    raw = _git(project_root, ["ls-files", "--full-name", "-z"])
    return [Path(part) for part in raw.split("\0") if part]


def _git_changed(project_root: Path, base_branch: str) -> set[Path]:
    """Paths changed since the merge base, relative to the REPO root."""
    try:
        merge_base = _git(project_root, ["merge-base", "HEAD", base_branch]).strip()
    except GitError as exc:
        # No shared ancestor: orphan branch, shallow clone, or the base branch is
        # absent entirely. Diffing HEAD against itself here would silently
        # resolve to zero files on a clean tree -- a scan that examined nothing
        # and said nothing about it. That is the failure mode this project
        # exists to forbid, so this is a hard stop, not a guess.
        raise GitError(
            f"No common ancestor between HEAD and {base_branch!r}, so there is "
            "no diff to scope to.\n"
            "This happens on an orphan branch, a shallow clone, or when the "
            "base branch is absent. Re-run with --full to sweep the whole "
            "repository, or set project.forge.base_branch to a branch that "
            "shares history."
        ) from exc
    # `--no-relative` is the counterpart to `--full-name` on ls-files: with
    # `diff.relative=true` set -- a documented, common monorepo config -- git
    # prints diff paths relative to the cwd instead of the repo root. The two
    # sets are intersected below, so one side drifting out of frame emptied it:
    # zero files, no error, no skip. Pin the frame rather than trust the config.
    raw = _git(project_root, ["diff", "--no-relative", "--name-only", "-z", merge_base])
    return {Path(part) for part in raw.split("\0") if part}


def resolve_files(
    project_root: Path,
    boundaries: BoundariesConfig,
    *,
    changed_only: bool = False,
    base_branch: str = "main",
) -> tuple[Path, ...]:
    """The files a run may examine, as paths relative to *project_root*.

    Raises GitError if a tracked file inside the boundaries has a name git
    could not hand back as valid UTF-8: it cannot be opened, and dropping it
    quietly would make the run report on less than it claims.
    """
    included = _spec(boundaries.include)
    excluded = _spec(boundaries.exclude) if boundaries.exclude else None

    prefix = _git_prefix(project_root)
    candidates = git_tracked(project_root)
    if changed_only:
        changed = _git_changed(project_root, base_branch)
        # Both sides are repo-root relative here. This is the only frame in
        # which the intersection is meaningful.
        candidates = [path for path in candidates if path in changed]

    kept: list[Path] = []
    undecodable: list[str] = []
    for repo_rel in candidates:
        posix = repo_rel.as_posix()
        if prefix:
            if not posix.startswith(prefix):
                # ls-files ran with cwd=project_root, so everything it returns
                # is under the prefix. If that ever stops being true, say so --
                # stripping blindly would put a file from a sibling project into
                # this run's scope.
                raise GitError(
                    f"`git ls-files` returned {ascii(posix)}, which is outside "
                    f"the project root {project_root}."
                )
            posix = posix[len(prefix) :]
        path = Path(posix)

        # Boundary matching comes before the disk check so that a file the run
        # was never going to open cannot fail the run over its name.
        if not included.match_file(posix):
            continue
        if excluded is not None and excluded.match_file(posix):
            continue
        if _LONE_SURROGATE.search(posix):
            undecodable.append(posix)
            continue
        # Tracked in the index but gone from disk -- deleted directly, or as
        # part of the very change under review. Either way there is no content
        # left for a lens to read, so it is dropped from the analysis set here
        # rather than surfacing as an unexplained FileNotFoundError downstream.
        if not (project_root / path).is_file():
            continue
        kept.append(path)

    if undecodable:
        raise GitError(
            "git returned path bytes that are not valid UTF-8, so these tracked "
            "files cannot be opened and were not examined:\n"
            + "\n".join(f"  {ascii(name)}" for name in undecodable)
            + "\nRename them to UTF-8 names, or add them to boundaries.exclude "
            "so the run is honest about not covering them."
        )
    # Sort by the POSIX string form, not bare Path comparison: WindowsPath
    # case-folds and PosixPath compares byte-wise, so plain `sorted(kept)`
    # orders the same repository differently per platform.
    return tuple(sorted(kept, key=lambda p: p.as_posix()))


def is_write_forbidden(
    path: Path, boundaries: BoundariesConfig, *, project_root: Path
) -> bool:
    """True when *path* may not be written to. ADVISORY ONLY -- see below.

    This classifies a path STRING, and the OS re-resolves that string at write
    time, so the answer describes the filesystem as it was when the question was
    asked. A junction planted in the window between the two redirects the write
    and this function is none the wiser: proven in the PR #4 gate, where the
    barrier returned False for `docs/deploy.yml`, a junction was planted
    pointing `docs` at `.github/workflows`, and the write landed on
    `.github/workflows/deploy.yml`. A hardlink defeats it outright, at any
    timing, because there is no link to follow and both names are equally
    canonical.

    Use it to REPORT ("this path is protected"), and `guarded_write` to WRITE.

    *path* may be relative to *project_root* or absolute; both are normalised
    before matching. This is the only place the write barrier is enforced, so
    it fails CLOSED: anything that cannot be shown to resolve to a location
    inside *project_root* is forbidden, whatever `never_touch` says. That covers
    an absolute path elsewhere, a `..` that escapes, a symlink pointing out of
    the worktree, and a Windows drive-relative path like `C:src` whose meaning
    depends on a per-drive current directory this process cannot know.
    """
    if path.drive and not path.root:
        # Drive-relative: "C:src" is "src under whatever the current directory
        # on C: happens to be". Unknowable here, so it does not get classified.
        return True

    try:
        root = project_root.resolve()
        target = (root / path).resolve()
    except (OSError, ValueError, RuntimeError):
        # Unresolvable: a symlink loop, a name the OS rejects, an unreachable
        # UNC host. Refuse rather than guess.
        return True

    try:
        relative = target.relative_to(root)
    except ValueError:
        return True

    # `.git` is refused unconditionally, at any depth, whatever the boundaries
    # say. It is the one directory inside the worktree where writing is not an
    # edit to the project but an edit to what the project DOES: a planted
    # `core.hooksPath` in `.git/config` runs attacker-chosen code on the next
    # ordinary git command, and `.git/hooks/` is more direct still.
    #
    # Unconditional because `never_touch` cannot be relied on to carry it. The
    # one production caller today, `report.write_report`, deliberately passes an
    # empty `BoundariesConfig` -- `--out` is the user naming a file, not
    # Whetstone editing a repository -- so `--out .git/config` reached
    # `guarded_write` and truncated it. Leaving this to a default entry in
    # `never_touch` would put it back one `BoundariesConfig()` away.
    #
    # Case-folded because Windows and macOS filesystems are, so `.GIT/config`
    # opens the same file, and every depth rather than the first component
    # because a submodule's git directory is nested.
    if any(part.lower() == ".git" for part in relative.parts):
        return True

    # A component the filesystem will rename out from under the match. Windows
    # strips trailing dots and spaces at the syscall, so `secrets.env.` is the
    # file `secrets.env` once it lands -- but `Path.resolve()` canonicalises
    # only the prefix that already EXISTS on disk, so a never_touch entry
    # guarding a file that has yet to be created (a secrets file, a lockfile, a
    # CI workflow -- the barrier's main purpose) was matched against one
    # spelling while the OS wrote another. Case folding does not rescue it; the
    # folded name carries the same decoration. Unconditional, not `os.name ==
    # "nt"`: a rule that only fires on Windows leaves the Linux CI leg unable to
    # catch a regression, and over-forbidding a legitimately-spaced POSIX name
    # costs one refused write where under-forbidding costs the barrier.
    # `:` is the NTFS alternate-data-stream separator and the same defect found
    # by inverting the case above: `secrets.env::$DATA` writes straight to the
    # main stream of `secrets.env`, and `secrets.env:hidden` creates that file
    # too. `resolve()` keeps the whole spelling as one component, so the barrier
    # compared `secrets.env::$DATA` against `secrets.env` and let it through.
    # Illegal in a Windows filename anyway, and a drive-relative path was
    # already refused above, so nothing legitimate reaches here carrying one.
    for part in relative.parts:
        if part in (".", ".."):
            continue
        if part != part.rstrip(" .") or ":" in part:
            return True

    if not boundaries.never_touch:
        return False

    posix = relative.as_posix()
    if _spec(boundaries.never_touch).match_file(posix):
        return True
    # gitwildmatch is case-sensitive; Windows and macOS filesystems are not, so
    # `INFRA/main.tf` opens the file `never_touch: ["infra/**"]` was protecting.
    # Fold and re-check everywhere rather than per platform: over-forbidding an
    # unrelated case variant on Linux costs one refused write, under-forbidding
    # costs the barrier.
    return _spec([p.lower() for p in boundaries.never_touch]).match_file(posix.lower())


# Flags that differ by platform, resolved once. `O_NOFOLLOW` does not exist on
# Windows and `O_BINARY` does not exist anywhere else; `getattr(..., 0)` is the
# no-op in each direction.
#
# `O_BINARY` is set on Windows so the descriptor performs no newline
# translation of its own -- the text wrapper opened over it does that, exactly
# once, and matches what `Path.write_text` produced before this existed.
_O_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_O_BINARY = getattr(os, "O_BINARY", 0)


def _identity(st: os.stat_result) -> tuple[int, int]:
    """What file this IS, independent of what it is called.

    `st_dev` and `st_ino` are populated on Windows as well as POSIX (CPython
    fills them from GetFileInformationByHandle), so this is one rule rather than
    a platform branch.
    """
    return (st.st_dev, st.st_ino)


def _open_for_write(target: Path) -> tuple[int, bool]:
    """Open *target* for writing without truncating it. Returns (fd, created).

    Two phases, because whether WE created the file decides whether a later
    refusal may remove it. `O_EXCL` answers that atomically; a prior
    `path.exists()` would be another check-then-act window in the function whose
    whole purpose is to close one.

    NOT `O_TRUNC`: truncation happens through the descriptor after verification.
    Truncating at open time destroys the contents of a protected file before the
    barrier has said a word about it.

    Only a refusal BY THE BARRIER becomes `WriteForbiddenError`. Every other
    `OSError` propagates unchanged. Wrapping them all made ENOSPC, EACCES and
    ENAMETOOLONG read as "the barrier refused you" and threw the errno away with
    `from None`, which is a worse diagnosis than the bare error.
    """
    base = os.O_RDWR | _O_NOFOLLOW | _O_BINARY
    try:
        return os.open(target, base | os.O_CREAT | os.O_EXCL), True
    except FileExistsError:
        pass
    except OSError as exc:
        _reraise_open_failure(target, exc)
    try:
        return os.open(target, base), False
    except OSError as exc:
        _reraise_open_failure(target, exc)


# What `O_NOFOLLOW` reports when the final component is a symlink. Linux says
# ELOOP; the BSDs, including macOS, say EMLINK. Both by NAME from `errno` -- the
# previous spelling was `getattr(os, "ELOOP", None)`, and `ELOOP` does not live
# on `os`, so it was always `None` and every `OSError` carrying `errno=None` was
# misreported as "is a symlink".
#
# POSIX-only and unverified from this Windows host, which is exactly why it is
# named constants rather than the raw numbers that sat here before: a wrong
# number fails silently, a wrong name fails at import.
_SYMLINK_REFUSED = frozenset({errno.ELOOP, errno.EMLINK})


def _reraise_open_failure(target: Path, exc: OSError) -> NoReturn:
    """Raise the barrier's error for a symlink refusal; re-raise anything else.

    `NoReturn`, not `None`, and it is load-bearing rather than decorative.
    `_open_for_write` ends both of its `except OSError` arms with a call to this
    function and is declared `-> tuple[int, bool]`. Typed `-> None`, those two
    arms read as falling off the end and returning `None`, which `guarded_write`
    would then unpack -- a `TypeError` about a non-sequence in place of the
    refusal. Only the raise in here prevents it, and `NoReturn` is what puts
    that fact in the signature instead of in a reader's head.

    THE LINE THIS DRAWS, because it was asked directly of the directory case:
    `WriteForbiddenError` means THE BARRIER REFUSED -- never_touch, containment,
    a symlink, a hardlink, an identity mismatch. Every other failure keeps its
    own type and its own errno.

    So EISDIR is deliberately NOT wrapped, and neither is EACCES, ENOSPC or
    ENAMETOOLONG. A directory is a user naming the wrong thing, not a security
    decision, and three reasons keep it on this side of the line:

    - Wrapping it re-blurs the boundary that was just fixed. These errors used
      to read as "the barrier refused you" with the errno discarded, which is
      how a full disk came to look like a policy violation.
    - The named, actionable error already exists at the layer that owns user
      input. `report.write_report` checks `is_dir()` before calling here and
      wraps any `OSError` from here as `ReportError`, so the user never sees a
      bare traceback whichever way this goes.
    - The other caller is M1b's implementer, which writes paths it computed
      itself. There a directory is a bug in the implementer, and
      `IsADirectoryError` with its errno is a better diagnosis than a
      security-flavoured sentence.

    The distinction is carried by TYPE, not by wording, so no caller has to
    match on a message to tell a refusal from a broken path.
    """
    if _O_NOFOLLOW and exc.errno in _SYMLINK_REFUSED:
        raise WriteForbiddenError(
            f"{target} is a symlink. Whetstone does not write through one: the "
            "link decides where the bytes land, on the repository's behalf "
            "rather than the caller's."
        ) from None
    raise exc


@contextlib.contextmanager
def guarded_write(
    path: Path,
    boundaries: BoundariesConfig,
    *,
    project_root: Path,
    encoding: str = "utf-8",
) -> Iterator[IO[str]]:
    """Open *path* for writing, verifying THROUGH THE DESCRIPTOR that it may be.

    The closure for the check-then-write gap in `is_write_forbidden`. That
    function classifies a path string and the OS re-resolves the string later;
    this one opens first and then asks what it is holding, so the thing verified
    and the thing written are the same object rather than the same spelling.

    Order, and why each step is where it is:

    1. `is_write_forbidden` up front. Cheap, and it produces the readable
       refusal for the ordinary case without creating anything.
    2. Open without `O_TRUNC`, with `O_EXCL` first so we learn atomically
       whether this call created the file.
    3. `fstat` the descriptor. `st_nlink > 1` is refused: a hardlink is a second
       name for these exact bytes, that name may be a protected one, and no
       path-string check can see it -- which is the residual issue #11 records
       against the old barrier. A non-regular file is refused too, which is what
       catches a Windows character device (`--out NUL` wrote successfully,
       discarded every byte, and left nothing on disk).
    4. `is_write_forbidden` AGAIN. `Path.resolve()` follows a junction, a
       symlink and a reparse-point chain alike, so a redirection planted before
       the open is visible here even though it was not visible at step 1.
    5. Confirm the path still names the file we are holding, by identity. If a
       redirection appeared between the open and step 4, the identities diverge
       and the write is refused. This direction FAILS CLOSED: the fd we hold is
       the honest file, and refusing it costs a write that would have been fine.
    6. Only now truncate, through the descriptor.

    On refusal, a file this call created is removed -- and only after confirming
    by identity that the name still refers to it, so a swap in that window
    cannot turn the cleanup into a deletion of somebody else's file. A file that
    already existed is left exactly as it was, un-truncated.

    WHAT THIS ACHIEVES PER PLATFORM, stated rather than implied:

    - POSIX: `O_NOFOLLOW` is passed, so a symlink at the FINAL component is
      refused by the kernel at open time, before anything else runs. Steps 3-5
      then cover intermediate components, hardlinks and devices.
    - Windows: there is no `O_NOFOLLOW` and no `openat`/`dir_fd`, so `os.open`
      follows a junction or symlink silently and the kernel refuses nothing.
      Every guarantee here is therefore post-open: the redirection is detected
      by step 4 and the identity check in step 5, after the descriptor exists.
      This is strictly weaker than the POSIX case in one specific way -- a
      redirection planted before the open is detected rather than prevented, so
      an empty file may briefly exist at the redirected location before step 6
      removes it. It is never written to, and it is removed before this function
      returns.

    THE RESIDUAL, on both platforms: this closes the window between the barrier
    and the write. It does not make the operation atomic against a concurrent
    attacker with write access to the worktree, because no filesystem API
    reachable from Python offers that on Windows. What it guarantees is that
    every divergence between the classified path and the opened file is detected
    and refused, in the fail-closed direction.

    WHAT IT RAISES, and the difference is load-bearing:

    - `WriteForbiddenError` means THE BARRIER REFUSED. never_touch, containment,
      a symlink, a hardlink, a device, an identity mismatch.
    - Any other `OSError` propagates with its type and errno intact -- a
      directory (EISDIR / EACCES), a missing parent (ENOENT), a full disk
      (ENOSPC), an over-long name (ENAMETOOLONG). None of those is a security
      decision and none of them should read as one. `_reraise_open_failure`
      argues this at length.

    A caller that needs to tell the two apart can do it on TYPE. A caller that
    wants one named error for a user -- `report.write_report` -- catches both
    and phrases them itself, which is where user-facing wording belongs.
    """
    root = project_root.resolve()
    if is_write_forbidden(path, boundaries, project_root=project_root):
        raise WriteForbiddenError(
            f"writing {path} is forbidden by the boundaries for {project_root}. "
            "It resolves outside the project root, or matches never_touch."
        )

    target = root / path
    fd, created = _open_for_write(target)
    try:
        _verify(fd, target, path, boundaries, project_root)
        # The identity the cleanup below gates on, taken while the descriptor is
        # still ours to ask. By the time the caller's body raises, `with handle`
        # has already closed it, so there is no fd left to `fstat` and this is
        # the only moment the answer is available.
        verified = _identity(os.fstat(fd))
        os.truncate(fd, 0)
        handle = os.fdopen(fd, "w", encoding=encoding)
    except BaseException:
        # ONE cleanup point, for everything that can go wrong before the handle
        # exists. `_verify` used to close the fd itself and then this block
        # closed it again -- `os.close` twice on the same number, harmless
        # single-threaded but a live hazard once M1a's provider makes threads
        # plausible, because the second close can land on a descriptor another
        # thread has since been given. It also only ran on a barrier refusal, so
        # an ENOSPC on `os.truncate` or a failure inside `fdopen` left a file
        # this call had created sitting on disk.
        _discard(fd, created, target)
        raise
    try:
        with handle:
            yield handle
    except BaseException:
        # The caller's body raised after being handed the descriptor. If this
        # call created the file, remove it -- `Path.write_text` was one call and
        # could not leave a partial file behind, and this must not be a
        # regression on that.
        #
        # A file that ALREADY EXISTED is left as it is, truncated and partial.
        # Its previous contents went at step 6 by design and cannot be brought
        # back; `write_text` had the same property, so nothing is lost that was
        # ever guaranteed.
        #
        # Identity-gated, exactly as `_discard` is. Without the gate, a
        # redirection planted while the caller's body was running turns the
        # tidy-up into a deletion of whatever the name points at now -- a worse
        # outcome than the partial file it is cleaning up, and the same
        # check-then-act shape this whole function exists to close. The two
        # cleanup paths had the same job and only one of them was gated.
        if created:
            with contextlib.suppress(OSError):
                if _identity(os.stat(target)) == verified:
                    os.unlink(target)
        raise


def _verify(
    fd: int,
    target: Path,
    path: Path,
    boundaries: BoundariesConfig,
    project_root: Path,
) -> None:
    """Refuse unless the descriptor is one this write is allowed to have.

    Raises and closes NOTHING -- the caller owns the descriptor and does all
    cleanup in one place. See `guarded_write` for the argument behind each check.
    """
    st = os.fstat(fd)
    # WHAT REACHES THIS BRANCH, measured on both platforms rather than reasoned
    # about, because an earlier version of this comment named a case that
    # executes nowhere:
    #
    #   character device -- REACHES IT on both. Windows opens `NUL` happily and
    #     reports S_IFCHR; Linux opens `/dev/null` and reports S_IFCHR. This is
    #     the case the branch exists for, and it is the dangerous one: the write
    #     SUCCEEDS, discards every byte, and reads as a completed write.
    #     Covered by `test_guarded_write_refuses_a_character_device`.
    #
    #   directory -- NEVER reaches it, on either platform. `os.open` fails
    #     first: EISDIR on Linux, EACCES on Windows. That is not wrapped (see
    #     `_reraise_open_failure`), so a directory surfaces as an OSError, which
    #     is what `test_guarded_write_refuses_a_directory` now asserts.
    #
    # The branch is kept because the device case is live and is the one where
    # silence would be mistaken for success.
    if not stat.S_ISREG(st.st_mode):
        raise WriteForbiddenError(
            f"{target} is not a regular file. Writing to a device succeeds "
            "while discarding every byte, which reads as a successful write."
        )
    if st.st_nlink > 1:
        raise WriteForbiddenError(
            f"{target} has more than one name on this filesystem (it is a "
            "hardlink). The other name may be a protected path, and no path "
            "check can see it -- both names are equally canonical and there is "
            "no link to follow."
        )
    if is_write_forbidden(path, boundaries, project_root=project_root):
        raise WriteForbiddenError(
            f"writing {path} became forbidden between the check and the open: "
            "it now resolves inside never_touch or outside the project root. A "
            "redirection was planted in that window."
        )
    try:
        landed = os.stat(target)
    except OSError as exc:
        raise WriteForbiddenError(
            f"{target} could not be re-examined after opening it ({exc}), so it "
            "cannot be shown to be the file that was verified."
        ) from None
    if _identity(landed) != _identity(st):
        raise WriteForbiddenError(
            f"{target} no longer names the file that was opened and verified. "
            "Refusing rather than writing to whichever of the two it is."
        )


def _discard(fd: int, created: bool, target: Path) -> None:
    """Close *fd* exactly once, and undo a creation this call made.

    The removal is identity-gated against the descriptor's own `fstat`, taken
    before the close. Without that, a redirection planted in this window would
    turn the tidy-up into a deletion of whatever the name points at now -- a
    worse outcome than the empty file it is cleaning up.
    """
    try:
        st = os.fstat(fd)
    except OSError:  # pragma: no cover - the fd was already unusable
        st = None
    with contextlib.suppress(OSError):
        os.close(fd)
    if created and st is not None:
        with contextlib.suppress(OSError):
            if _identity(os.stat(target)) == _identity(st):
                os.unlink(target)
