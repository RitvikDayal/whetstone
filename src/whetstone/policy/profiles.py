"""The per-stage permission profiles.

Behaviour lives in declarative units. A stage's powers are read off this table,
not assembled in code, so an audit is a diff of this file.

READ `available_tools` AS THE WHOLE LIST. It is not "these and the defaults";
it is the complete set of tools that exist for that stage. `denied_tools` is
kept alongside it and is deliberately redundant: a name that is not available
cannot be called anyway, so the deny list only bites if a future CLI release
changes what `--tools` means. It cost nothing and the last time this module
trusted one flag's meaning it was wrong.

NO M1a STAGE GETS A SHELL, and that is a decision rather than an oversight.

An earlier version granted `Bash` to `reproduce` and `falsify` with an empty
`bash_allowlist`, on the belief that an unapproved tool is a refused tool. That
was measured wrong. Refusal was sampled with a prompt that asked the model to
CREATE A FILE, so every attempt was mutating, and mutating commands really are
refused and really are recorded. A reviewer ran the same profile asking for
read-only commands and got `echo`, `ls`, `cat README.md`, `find`, `git log` and
`git config --get user.email` to EXECUTE -- six commands, `permission_denials:
[]`, and the stage reported a clean success.

The CLI carries its own read-only-command classifier that auto-approves
independently of `--allowedTools` and records nothing when it does. So an empty
`bash_allowlist` bounds nothing; the real bound is an undocumented heuristic
inside somebody else's binary. `cat .env` is the sharp end, and reads are not
confined to the worktree either.

`bash_allowlist` stays on `PermissionSet` because it is still the right shape --
but its consumer is the CONTROLLER, not the model. M1a's invariant 2 already
says the deterministic layer holds authority and a model's self-assessment is
recomputed from the world; a stage running its own repro command was always in
tension with that. Task 7 executes the command itself and hands the stage the
result.
"""

from __future__ import annotations

from pathlib import Path

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

# Read-only inspection, and the whole of it. Auto-approved because a prompt in
# `-p` mode has nobody to answer it and becomes a silent refusal.
_INSPECT = frozenset({"Read", "Grep", "Glob"})

# Never grantable in M1a. `Bash` for the reason in the module docstring; `Agent`
# and `TaskCreate` because a subagent is an unbounded tool set reachable from a
# bounded one -- a reviewer spawned one from a profile that granted neither, and
# when its `Write` was refused it fell back to `Bash` and wrote the file anyway.
FORBIDDEN_IN_M1A = frozenset({"Bash", "Agent", "TaskCreate"})

_READ_ONLY = PermissionSet(
    available_tools=_INSPECT,
    auto_approve=_INSPECT,
    denied_tools=_NO_WRITES,
    bash_allowlist=frozenset(),
    read_denied=_READ_DENIED,
    write_root=None,
)

# All three stages hold the same powers and differ only in their prompt. That is
# the honest shape of M1a: the separation between hunt, reproduce and falsify is
# what each is ASKED, plus the process boundary between them, not what each may
# touch.
PROFILES: dict[str, PermissionSet] = {
    "hunt": _READ_ONLY,
    "reproduce": _READ_ONLY,
    "falsify": _READ_ONLY,
}


def profile_for(stage: str) -> PermissionSet:
    """Return the profile for *stage*, refusing an unknown one.

    Defaulting to a permissive set would turn a typo into a privilege
    escalation, so an unknown stage is an error rather than a fallback.
    """
    if stage == "implement":
        raise PolicyError(
            "the implement profile is per-run and cannot be looked up by "
            "name: it is scoped to that run's worktree. Call "
            "`implement_profile(worktree)`."
        )
    try:
        return PROFILES[stage]
    except KeyError as exc:
        raise PolicyError(
            f"no permission profile for stage {stage!r}. "
            f"Known stages: {', '.join(sorted(PROFILES))}."
        ) from exc


# Read-only inspection PLUS the two tools that write. Not `NotebookEdit`: a
# notebook is not what this stage fixes, and every tool granted is one whose
# behaviour has to be understood before it can be bounded.
_WRITE = frozenset({"Edit", "Write"})
_IMPLEMENT_TOOLS = _INSPECT | _WRITE


def implement_profile(worktree: Path | None) -> PermissionSet:
    """The first profile in this project that may write. Scoped to *worktree*.

    THIS IS WHERE M1a's POSTURE CHANGES, for exactly one stage and by exactly
    two tools. D21 gave no M1a stage a shell and every stage since has held
    `Read`, `Grep`, `Glob` and nothing else; an implementer that cannot write
    cannot implement.

    WHAT DOES NOT CHANGE: `Bash`, `Agent` and `TaskCreate` stay forbidden, and
    that is what keeps the write root meaningful. A shell can write anywhere,
    so granting one would make `--add-dir` advisory; and a subagent is an
    unbounded tool set reachable from a bounded one -- a reviewer spawned one
    from a profile granting neither, and when its `Write` was refused it fell
    back to `Bash` and wrote the file anyway.

    A WRITING PROFILE WITH NO WRITE ROOT IS REFUSED. `write_root=None` is what
    every read-only profile carries and it means "no writes are expected".
    Combined with `Write` it reads as bounded and is not -- the exact shape of
    the `--allowedTools` defect that cost M1a three revisions.

    THE RESIDUAL, live rather than latent now that a stage can write:
    `--add-dir` scopes WRITES only. Reads stay unbounded filesystem-wide, so a
    stage that can write is a stage that can copy what it read into a file it
    writes. Closing that needs a process boundary and is not this task's.
    """
    if worktree is None:
        raise PolicyError(
            "an implement profile needs a write root -- the run's worktree. A "
            "profile holding Write with no root reads as bounded and is not."
        )
    return PermissionSet(
        available_tools=_IMPLEMENT_TOOLS,
        auto_approve=_IMPLEMENT_TOOLS,
        denied_tools=FORBIDDEN_IN_M1A,
        bash_allowlist=frozenset(),
        read_denied=_READ_DENIED,
        write_root=worktree,
    )
