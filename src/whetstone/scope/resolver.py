"""Resolve boundaries + git state into the files a run may examine.

`include` and `exclude` decide what is *analysed*. `never_touch` decides what may
be *written*, and deliberately does not filter analysis — a finding in a
protected path is still worth telling a human about.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pathspec

from ..config.model import BoundariesConfig
from ..errors import GitError


def _spec(patterns: list[str]) -> pathspec.PathSpec:
    return pathspec.PathSpec.from_lines("gitwildmatch", patterns)


def _git(project_root: Path, args: list[str]) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=project_root,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        raise GitError(
            f"`git {' '.join(args)}` failed in {project_root}: "
            f"{proc.stderr.strip() or 'no stderr'}"
        )
    return proc.stdout


def git_tracked(project_root: Path) -> list[Path]:
    raw = _git(project_root, ["ls-files", "-z"])
    return [Path(part) for part in raw.split("\0") if part]


def _git_changed(project_root: Path, base_branch: str) -> set[Path]:
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
    raw = _git(project_root, ["diff", "--name-only", "-z", merge_base])
    return {Path(part) for part in raw.split("\0") if part}


def resolve_files(
    project_root: Path,
    boundaries: BoundariesConfig,
    *,
    changed_only: bool = False,
    base_branch: str = "main",
) -> tuple[Path, ...]:
    included = _spec(boundaries.include)
    excluded = _spec(boundaries.exclude) if boundaries.exclude else None

    candidates = git_tracked(project_root)
    if changed_only:
        changed = _git_changed(project_root, base_branch)
        candidates = [path for path in candidates if path in changed]

    kept = []
    for path in candidates:
        # Tracked in the index but gone from disk -- deleted directly, or as
        # part of the very change under review. Either way there is no content
        # left for a lens to read, so it is dropped from the analysis set here
        # rather than surfacing as an unexplained FileNotFoundError downstream.
        if not (project_root / path).is_file():
            continue
        posix = path.as_posix()
        if not included.match_file(posix):
            continue
        if excluded is not None and excluded.match_file(posix):
            continue
        kept.append(path)
    # Sort by the POSIX string form, not bare Path comparison: WindowsPath
    # case-folds and PosixPath compares byte-wise, so plain `sorted(kept)`
    # orders the same repository differently per platform.
    return tuple(sorted(kept, key=lambda p: p.as_posix()))


def is_write_forbidden(rel_path: Path, boundaries: BoundariesConfig) -> bool:
    """True when *rel_path* is inside a never_touch pattern."""
    if not boundaries.never_touch:
        return False
    return _spec(boundaries.never_touch).match_file(rel_path.as_posix())
