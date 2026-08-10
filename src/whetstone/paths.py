"""Where Whetstone keeps its state, and where it refuses to."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path

from .errors import UnsafeStatePathError

# Substrings that indicate a file-replacing sync client owns the directory.
# SQLite in WAL mode needs its -wal and -shm sidecars to stay consistent with
# the main file; sync clients replace files independently and tear the database.
_CLOUD_MARKERS = (
    "onedrive",
    "dropbox",
    "google drive",
    "googledrive",
    "cloudstorage",
    "icloud",
    "box sync",
    "pcloud",
    "nextcloud",
    "syncthing",
)


def assert_not_cloud_synced(path: Path) -> None:
    """Raise if *path* looks like it lives inside a cloud-sync root."""
    lowered = str(path).lower().replace("\\", "/")
    for marker in _CLOUD_MARKERS:
        if marker in lowered:
            raise UnsafeStatePathError(
                f"State path {path} looks cloud-synced (matched {marker!r}).\n"
                "SQLite write-ahead-log files are torn by sync clients that replace "
                "whole files, and the corruption is silent.\n"
                "Set `state_dir` in whetstone.yaml to a local path."
            )


def state_root(project_root: Path, override: str | None = None) -> Path:
    """Resolve, validate, and create the state directory for *project_root*."""
    if override:
        root = Path(os.path.expandvars(override)).expanduser()
    else:
        digest = hashlib.sha256(
            str(project_root.resolve()).encode("utf-8")
        ).hexdigest()[:12]
        root = Path.home() / ".whetstone" / digest
    root = root.resolve() if root.exists() else root.absolute()
    assert_not_cloud_synced(root)
    root.mkdir(parents=True, exist_ok=True)
    return root
