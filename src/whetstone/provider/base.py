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


class ProviderError(WhetstoneError):
    """A provider could not run a stage at all."""


@dataclass(frozen=True)
class Usage:
    """What a stage cost. Zero means free, never unknown -- an unknown cost
    that defaults to zero silently under-reports, so a provider that cannot
    measure must say so by leaving cost_usd None rather than by omitting Usage.
    """

    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float | None = None
    wall_seconds: float = 0.0


@dataclass(frozen=True)
class StageRequest:
    stage: str
    prompt: str
    schema: dict[str, Any]
    permissions: Any
    effort: str
    max_turns: int
    cwd: Path


@dataclass(frozen=True)
class StageResult:
    ok: bool
    data: dict[str, Any] | None
    raw: str
    usage: Usage
    error: str | None

    def __post_init__(self) -> None:
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


@runtime_checkable
class Provider(Protocol):
    name: str

    def run_stage(self, request: StageRequest) -> StageResult: ...
