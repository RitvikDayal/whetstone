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

import re
import subprocess
from pathlib import Path

import pathspec

from ..config.model import BoundariesConfig
from ..errors import GitError

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
    """True when *path* may not be written to.

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
