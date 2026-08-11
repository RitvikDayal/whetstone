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
    except GitError:
        # No shared ancestor (shallow clone, or base branch absent). Fall back to
        # the working tree diff rather than silently returning everything.
        merge_base = "HEAD"
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
        posix = path.as_posix()
        if not included.match_file(posix):
            continue
        if excluded is not None and excluded.match_file(posix):
            continue
        kept.append(path)
    return tuple(sorted(kept))


def is_write_forbidden(rel_path: Path, boundaries: BoundariesConfig) -> bool:
    """True when *rel_path* is inside a never_touch pattern."""
    if not boundaries.never_touch:
        return False
    return _spec(boundaries.never_touch).match_file(rel_path.as_posix())
