"""Deny by default.

TWO SETS, AND THEY ARE NOT THE SAME SET. This module's first version had one
field named `allowed_tools`, which the provider mapped onto the CLI's
`--allowedTools`. The names match and the meanings are opposite:

    --tools         the tools that EXIST for this stage        <- the bound
    --allowedTools  the tools that do not need approving       <- convenience

So every stage ran with the CLI's full default toolset. An adversarial reviewer
drove a `reproduce` stage into creating files, appending to README.md and
running `git init`, and `permission_denials` came back empty because nothing
was ever refused -- there was nothing to refuse. The fields are now
`available_tools` and `auto_approve`, renamed rather than reused, because the
old name is what made the wrong mapping read as correct.

Bash allowlisting is exact match after whitespace normalisation, never prefix
matching. A prefix allowlist is bypassable -- `pytest; rm -rf /` starts with
`pytest` -- and without exactness "a read-only stage with Bash" is a fiction
rather than a property.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from ..errors import WhetstoneError


class PolicyError(WhetstoneError):
    """A stage asked for a permission profile that does not exist, or asked to
    run with no policy at all."""


# Everything Python calls whitespace EXCEPT space and tab. `str.split()` splits
# on all of it, which is why the first `_normalise` was bypassable: `\n` is a
# POSIX command separator, so `bash\nscripts/ci.sh` normalised to the
# allowlisted `bash scripts/ci.sh` and ran two commands. `\x1c`-`\x1f` and
# `\x85` and `\xa0` are all `isspace()` in Python and all reachable the same
# way. The parametrised test covered `;`, `&&` and `|` and missed the one
# separator normalisation actually erases.
_FOREIGN_SPACE = re.compile(r"[^\S \t]")
_RUNS = re.compile(r"[ \t]+")


def _base_tool(spec: str) -> str:
    """`Bash(uv run pytest -q)` -> `Bash`. The scoped form the CLI accepts in
    `--allowedTools` names the tool it scopes, and that is the name that has to
    appear in `--tools` for the scope to mean anything."""
    return spec.split("(", 1)[0].strip()


@dataclass(frozen=True)
class PermissionSet:
    """What one stage may do.

    `available_tools` becomes `--tools` and IS the bound: a name absent from it
    does not exist for that stage. `auto_approve` becomes `--allowedTools` and
    only removes an approval prompt -- it can never widen `available_tools`,
    and `__post_init__` enforces that rather than asserting it, since the
    superseded design's whole failure was a claim nothing checked.
    """

    available_tools: frozenset[str]
    auto_approve: frozenset[str]
    denied_tools: frozenset[str]
    bash_allowlist: frozenset[str]
    read_denied: tuple[str, ...]
    write_root: Path | None

    def __post_init__(self) -> None:
        widened = {
            spec
            for spec in self.auto_approve
            if _base_tool(spec) not in self.available_tools
        }
        if widened:
            raise PolicyError(
                f"auto_approve names {sorted(widened)}, which is not in "
                f"available_tools. Approving a tool the stage does not have is "
                f"either a typo or an attempt to widen the bound through the "
                f"convenience flag; both are refused."
            )
        # Both sides normalised through `_base_tool`. Comparing raw strings
        # here while the check above normalises meant `denied_tools={"Bash(rm
        # -rf /)"}` alongside `available_tools={"Bash"}` reported no overlap --
        # two spellings of the same tool, one of them a deny, silently passing.
        denied_bases = {_base_tool(spec) for spec in self.denied_tools}
        overlap = {
            spec
            for spec in self.available_tools
            if _base_tool(spec) in denied_bases
        }
        if overlap:
            raise PolicyError(
                f"{sorted(overlap)} is both available and denied. The CLI's "
                f"resolution order between the two is not a thing to rely on."
            )


def _normalise(command: str) -> str:
    return _RUNS.sub(" ", command).strip()


def bash_permitted(command: str, permissions: PermissionSet) -> bool:
    """True only when *command* is exactly an allowlisted command.

    Runs of space and tab are collapsed so that formatting differences do not
    matter. NOTHING ELSE IS NORMALISED, and in particular any other whitespace
    character makes the command refused outright rather than normalised away:
    a newline is a command separator, so erasing it turns one allowlisted
    command into two arbitrary ones.

    No prefix, no substring, no shell decomposition. A command containing a
    separator is a different command, and this returns False for it.
    """
    if _FOREIGN_SPACE.search(command):
        return False
    normalised = _normalise(command)
    return normalised in {
        _normalise(entry)
        for entry in permissions.bash_allowlist
        if not _FOREIGN_SPACE.search(entry)
    }
