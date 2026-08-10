"""Where Whetstone keeps its state, and where it refuses to."""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from .errors import ConfigError, StateDirError, UnsafeStatePathError

# Substrings that indicate a file-replacing sync client owns the directory.
# SQLite in WAL mode needs its -wal and -shm sidecars to stay consistent with
# the main file; sync clients replace files independently and tear the database.
# Matched against the path lowercased with backslashes folded to "/".
_CLOUD_MARKERS = (
    "onedrive",
    "dropbox",
    "google drive",
    "googledrive",
    "my drive",  # Google Drive for Desktop, Windows lettered mount
    "shared drives",  # ...and its shared-drive half
    "cloudstorage",
    "icloud",  # the Windows `iCloudDrive` folder
    "mobile documents",  # the real macOS iCloud Drive path
    "com~apple~clouddocs",
    "box sync",
    "pcloud",
    "nextcloud",
    "syncthing",
    "resilio sync",
    "seafile",
    "tresorit",
    "sync.com",
)

# Markers that must be a whole path component. "mega" as a bare substring hits
# "omega" and "megabyte", which are ordinary directory names.
_CLOUD_COMPONENTS = frozenset({"mega"})

# An unexpanded variable left over after os.path.expandvars — the name was not
# set in the environment. Only the $VAR and ${VAR} forms are checked: they are
# the cross-platform spelling, and %VAR% is never expanded on POSIX, so treating
# it as an error there would reject legal path characters.
_UNEXPANDED_VAR = re.compile(r"\$\{?[A-Za-z_][A-Za-z0-9_]*\}?")


def assert_not_cloud_synced(path: Path) -> None:
    """Raise if *path* looks like it lives inside a cloud-sync root."""
    lowered = str(path).lower().replace("\\", "/")
    matched = next((m for m in _CLOUD_MARKERS if m in lowered), None)
    if matched is None:
        components = set(lowered.split("/"))
        matched = next((m for m in _CLOUD_COMPONENTS if m in components), None)
    if matched is not None:
        raise UnsafeStatePathError(
            f"State path {path} looks cloud-synced (matched {matched!r}).\n"
            "SQLite write-ahead-log files are torn by sync clients that replace "
            "whole files, and the corruption is silent.\n"
            "Set `state_dir` in whetstone.yaml to a local path."
        )


def state_root(project_root: Path, override: str | None = None) -> Path:
    """Resolve, validate, and create the state directory for *project_root*."""
    if override is None:
        digest = hashlib.sha256(
            str(project_root.resolve()).encode("utf-8")
        ).hexdigest()[:12]
        root = Path.home() / ".whetstone" / digest
    else:
        root = _root_from_override(override)
    root = root.resolve()
    assert_not_cloud_synced(root)
    _make_dir(root, override)
    return root


def _root_from_override(override: str) -> Path:
    """Turn a configured `state_dir` into a path, refusing the silent failures.

    An empty value used to be falsy and fall through to the home-hash directory,
    so a config that meant to relocate state was ignored without a word. An
    unset variable used to survive `expandvars` and become a directory literally
    named `$XDG_STATE_HOME`. `loader._substitute` already errors on an unset
    ${env:VAR}; these two behave the same way now.
    """
    if not override.strip():
        raise ConfigError(
            "`state_dir` in whetstone.yaml is empty.\n"
            "Give it a path, or remove the key to use the default under "
            f"{Path.home() / '.whetstone'}."
        )
    expanded = os.path.expandvars(override)
    leftover = _UNEXPANDED_VAR.search(expanded)
    if leftover is not None:
        name = leftover.group(0).lstrip("$").strip("{}")
        raise ConfigError(
            f"`state_dir` is {override!r} but {name} is not set in the "
            "environment.\n"
            f"Creating it would make a directory literally named "
            f"{leftover.group(0)!r}."
        )
    return Path(expanded).expanduser()


def _make_dir(root: Path, override: str | None) -> None:
    try:
        root.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise StateDirError(_state_dir_message(root, override, exc)) from exc


def _state_dir_message(root: Path, override: str | None, exc: OSError) -> str:
    source = (
        f"`state_dir: {override}` in whetstone.yaml"
        if override is not None
        else "the default state directory"
    )
    blocker = _first_non_directory(root)
    if blocker == root:
        return (
            f"{source} resolves to {root}, which is an existing file.\n"
            "State needs a directory. This is usually a typo — point `state_dir` "
            "at a directory, or move the file out of the way."
        )
    if blocker is not None:
        return (
            f"{source} resolves to {root}, but {blocker} is a file, not a "
            "directory.\n"
            "A directory cannot be created underneath it. Point `state_dir` "
            "somewhere else."
        )
    return f"{source} resolves to {root}, which could not be created: {exc}"


def _first_non_directory(root: Path) -> Path | None:
    """The shallowest ancestor of *root* (or *root*) that exists but is not a dir."""
    for candidate in (*reversed(root.parents), root):
        if candidate.exists() and not candidate.is_dir():
            return candidate
    return None
