"""The contract a sink implements, and the record of what it published.

A SINK IS TOLD, IT DOES NOT DECIDE. It receives a finding that has already been
graded, decided about and -- for a PR -- verified. It never chooses whether to
publish, never re-grades, and never edits the text: those are the spine's job
and a sink that did any of them would be a second place where policy lives.

PUBLICATION IS RECORDED, INCLUDING WHEN IT FAILS. A sink that could not reach
its service must say so in a way that reaches the user, because "published" and
"tried to publish" are the difference between a finding somebody will see and
one nobody will.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

from ..errors import WhetstoneError


class SinkError(WhetstoneError):
    """A sink that could not publish, and why."""


@dataclass(frozen=True)
class Publication:
    """What a sink did, or could not do.

    `url` is None on every failure path, and `reason` is never None on one --
    a Publication that says neither where it went nor why it did not is the
    silent-failure shape this project refuses everywhere else.
    """

    published: bool
    kind: str
    url: str | None = None
    reason: str | None = None
    # Whatever the sink wants on the record: a PR number, an issue id, the
    # exact argv. Read by nothing, kept so a failure is diagnosable.
    detail: dict[str, Any] | None = None

    def __post_init__(self) -> None:
        if self.published and not self.url:
            raise SinkError(
                f"{self.kind} reported a publication with no URL. A publication "
                "nobody can open is not one, and every caller renders this as a "
                "link."
            )
        if not self.published and not self.reason:
            raise SinkError(
                f"{self.kind} reported a failure with no reason. 'It did not "
                "work' is the message this project exists to stop producing."
            )


@runtime_checkable
class Sink(Protocol):
    """Publish one decided finding. Never decides whether it should be."""

    kind: str

    def publish(self, finding: Any, *, dry_run: bool = False) -> Publication: ...
