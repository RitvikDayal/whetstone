"""What a hygiene detector must implement."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol, runtime_checkable

from ...base import Candidate, RunContext


@runtime_checkable
class Detector(Protocol):
    id: str

    def detect(self, ctx: RunContext) -> Iterator[Candidate]: ...
