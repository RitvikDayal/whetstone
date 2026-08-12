"""Deny by default.

Bash allowlisting is exact match after whitespace normalisation, never prefix
matching. A prefix allowlist is bypassable -- `pytest; rm -rf /` starts with
`pytest` -- and without exactness "a read-only stage with Bash" is a fiction
rather than a property.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from ..errors import WhetstoneError


class PolicyError(WhetstoneError):
    """A stage asked for a permission profile that does not exist."""


@dataclass(frozen=True)
class PermissionSet:
    allowed_tools: frozenset[str]
    denied_tools: frozenset[str]
    bash_allowlist: frozenset[str]
    read_denied: tuple[str, ...]
    write_root: Path | None


def _normalise(command: str) -> str:
    return " ".join(command.split())


def bash_permitted(command: str, permissions: PermissionSet) -> bool:
    """True only when *command* is exactly an allowlisted command.

    Whitespace is normalised so that formatting differences do not matter.
    Nothing else is: no prefix, no substring, no shell decomposition. A command
    containing a separator is a different command, and this returns False for it.
    """
    return _normalise(command) in {_normalise(a) for a in permissions.bash_allowlist}
