"""What every provider implements.

A provider runs one stage and returns structured output. It does not decide
anything: it does not judge whether the output is good, retry on a bad verdict,
or interpret the payload. Those belong to the spine, which is the only layer
allowed to conclude something.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from ..errors import WhetstoneError
from ..policy.gate import PermissionSet


class ProviderError(WhetstoneError):
    """A provider could not run a stage at all."""


@dataclass(frozen=True)
class Usage:
    """What a stage cost. Zero means free, never unknown -- an unknown cost
    that defaults to zero silently under-reports, so a provider that cannot
    measure must say so by leaving cost_usd None rather than by omitting Usage.

    The cache fields are not optional detail. A measured trivial call reported
    `input_tokens: 4` alongside `cache_creation_input_tokens: 41036`, because
    the subprocess inherits whatever configuration the operator has installed.
    A budget reading `input_tokens` alone would have under-reported that stage
    by four orders of magnitude, so anything summing tokens must sum all four.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cost_usd: float | None = None
    wall_seconds: float = 0.0
    # Which key in the envelope these numbers came from. A budget-exhausted
    # envelope carried an all-zero top-level `usage` alongside a `modelUsage`
    # reporting 47,661 cache-creation tokens, so "where did this come from" is
    # a question a cost report has to be able to answer.
    source: str = "none"

    @property
    def total_tokens(self) -> int:
        """Every token the stage was billed for, cached or not.

        Exists so no caller has to remember the four-field shape, which is
        exactly the mistake that makes a budget under-report.
        """
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_input_tokens
            + self.cache_read_input_tokens
        )


@dataclass(frozen=True)
class StageRequest:
    """One stage's ask. Everything the provider needs and nothing it decides.

    `max_budget_usd` rather than a turn count, because the Claude Code CLI has
    no `--max-turns` and does have `--max-budget-usd`. The first spelling was
    written before anybody ran the binary; a request field no provider consumes
    is a bound the caller believes it set and nothing enforces, which is the
    same defect as a code path that declines work without saying so.

    None means unbounded, and that is a real choice rather than a missing value:
    Task 9's budget holds the run-level ceiling, so a stage that declines to set
    its own is deferring to it, not escaping it.

    `permissions` is typed rather than `Any`. It was `Any`, and a stage built
    with `permissions=None` produced a command line with no permission flags on
    it at all -- the absence of a policy and the most restrictive policy
    expressible both yielding the least restrictive invocation.
    """

    stage: str
    prompt: str
    schema: dict[str, Any]
    permissions: PermissionSet
    effort: str
    max_budget_usd: float | None
    cwd: Path


@dataclass(frozen=True)
class StageResult:
    """What one stage produced, and what it cost to find out.

    `denials` and `mutation` are both observations about the run rather than
    about the payload, and both exist because the provider's first version
    reported neither. A stage whose tool call was refused returned
    `ok=True, error=None`; a stage that modified the worktree returned the same.
    """

    ok: bool
    data: dict[str, Any] | None
    raw: str
    usage: Usage
    error: str | None
    # What the CLI refused. Necessary and NOT sufficient: measured against the
    # real binary, a tool left out of `--tools` produces an empty
    # `permission_denials`, because there was nothing to refuse. Absence is
    # invisible here; only `mutation` sees through it.
    denials: tuple[str, ...] = ()
    # What changed in the worktree while the stage ran, or None. Every M1a
    # stage is read-only, so anything at all here is a defect.
    mutation: str | None = None
    # How many turns the CLI took. One turn means the model answered without
    # calling a single tool -- which is what a FABRICATED answer looks like, and
    # what a blanket refusal looks like. Proven: a stage asked for the installed
    # git version "measured by running it" returned an invented version number,
    # schema-valid, no denials, no mutation. The sentinel structurally cannot
    # see that; a fabricated READ changes nothing on disk.
    #
    # Recorded here and judged NOWHERE in this module. Invariant 2 says the
    # provider decides nothing, so what counts as a substantive stage is the
    # lens's call, not the adapter's.
    turns: int = 0

    def __post_init__(self) -> None:
        if not self.ok and self.error is None:
            raise ValueError(
                "a failed StageResult must carry an error -- a failure with no "
                "reason is a path that declines to do work and says nothing, "
                "which is the shape this repo bans everywhere else"
            )
        if not self.ok and self.data is not None:
            raise ValueError(
                "a failed StageResult must not carry data -- the spine reads "
                "data when ok is true and would act on a payload the provider "
                "itself does not stand behind"
            )
        if self.ok and self.error is not None:
            raise ValueError(
                "a successful StageResult must not carry an error -- a caller "
                "checking ok would never see it"
            )
        if self.ok and self.data is None:
            raise ValueError(
                "a successful StageResult must carry data -- a success with no "
                "payload is a path that declined to do work and said nothing, "
                "and the spine dereferences data whenever ok is true"
            )


@runtime_checkable
class Provider(Protocol):
    name: str

    def run_stage(self, request: StageRequest) -> StageResult: ...
