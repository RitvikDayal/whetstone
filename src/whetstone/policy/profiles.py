"""The per-stage permission profiles.

Behaviour lives in declarative units. A stage's powers are read off this table,
not assembled in code, so an audit is a diff of this file.
"""

from __future__ import annotations

from .gate import PermissionSet, PolicyError

_READ_DENIED = (
    ".env*",
    "**/secrets/**",
    "**/credentials/**",
    "**/.ssh/**",
    "**/.aws/**",
    "**/.kube/**",
)

_NO_WRITES = frozenset({"Edit", "Write", "NotebookEdit"})

PROFILES: dict[str, PermissionSet] = {
    "hunt": PermissionSet(
        allowed_tools=frozenset({"Read", "Grep", "Glob"}),
        denied_tools=_NO_WRITES,
        bash_allowlist=frozenset(),
        read_denied=_READ_DENIED,
        write_root=None,
    ),
    "reproduce": PermissionSet(
        allowed_tools=frozenset({"Read", "Grep", "Glob", "Bash"}),
        denied_tools=_NO_WRITES,
        bash_allowlist=frozenset(),
        read_denied=_READ_DENIED,
        write_root=None,
    ),
    "falsify": PermissionSet(
        allowed_tools=frozenset({"Read", "Grep", "Glob", "Bash"}),
        denied_tools=_NO_WRITES,
        bash_allowlist=frozenset(),
        read_denied=_READ_DENIED,
        write_root=None,
    ),
}


def profile_for(stage: str) -> PermissionSet:
    """Return the profile for *stage*, refusing an unknown one.

    Defaulting to a permissive set would turn a typo into a privilege
    escalation, so an unknown stage is an error rather than a fallback.
    """
    try:
        return PROFILES[stage]
    except KeyError as exc:
        raise PolicyError(
            f"no permission profile for stage {stage!r}. "
            f"Known stages: {', '.join(sorted(PROFILES))}."
        ) from exc
