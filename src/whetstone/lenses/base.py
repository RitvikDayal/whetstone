"""The contract every lens pack implements.

A lens produces candidates and evidence. It never touches git, never publishes
to a sink, never writes to the store, and never decides its own autonomy.
Everything with consequences belongs to the spine.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

# Re-exported: `Severity` is shared with `config`, which must not import the
# lens layer. See whetstone/severity.py.
from ..severity import Severity as Severity
from ..severity import severity_at_least as severity_at_least


class LensScope(StrEnum):
    """Whether a lens is narrowed by `boundaries`, or reads fixed artifacts.

    `file` means the lens works from `RunContext.files`, so
    `boundaries.include` and `boundaries.exclude` decide what it examines.
    `project` means it reads paths it picks itself -- coverage.xml, a
    dependency manifest -- and those patterns do not apply to it.

    The distinction is not cosmetic: a user who writes
    `exclude: ["coverage.xml"]` and still gets a finding about coverage.xml
    has been told something false by silence.
    """

    file = "file"
    project = "project"


def lens_scope(pack: LensPack) -> LensScope:
    """A pack's declared scope, defaulting to file-scoped.

    Deliberately NOT a member of the `LensPack` protocol. `runtime_checkable`
    isinstance() checks every protocol attribute, so requiring `scope` there
    would make `register()` reject every pack written before this existed --
    including third-party ones already installed.

    The default is `file` rather than `project` because the two failure modes
    are not symmetric. Defaulting to `project` puts a "this lens ignored your
    boundaries" line on every honest file-scoped lens, and a report full of
    known-false lines is one nobody reads. Defaulting to `file` costs one
    missing advisory line for a project-scoped pack whose author did not
    declare it.
    """
    try:
        return LensScope(getattr(pack, "scope", LensScope.file))
    except ValueError:
        return LensScope.file


class EvidenceKind(StrEnum):
    metric = "metric"      # a measured value crossing a threshold
    repro = "repro"        # an executable artifact that fails before, passes after
    capture = "capture"    # a screenshot plus replayable navigation
    critique = "critique"  # a judgement with a cited heuristic — never provable


@dataclass(frozen=True)
class Evidence:
    kind: EvidenceKind
    summary: str
    data: dict[str, Any]
    artifacts: tuple[str, ...] = ()

    def to_json(self) -> str:
        """Deterministic JSON — key order must not affect stored bytes."""
        return json.dumps(
            {
                "kind": str(self.kind),
                "summary": self.summary,
                "data": self.data,
                "artifacts": list(self.artifacts),
            },
            sort_keys=True,
        )


@dataclass(frozen=True)
class Candidate:
    """What a lens produces. The spine turns it into a Finding."""

    lens: str
    rule_id: str
    subject: str  # file path, package name, route — what this is *about*
    title: str
    detail: str
    severity: Severity
    evidence: Evidence

    @property
    def dedupe_key(self) -> str:
        """Identity of the finding, independent of wording.

        Deliberately excludes title, detail, and severity so that a reworded or
        re-scored candidate is recognised as the same finding and cannot
        resurrect a rejection.

        JSON-encodes the components rather than joining them on a separator:
        `subject` holds file paths, package names, and routes, and a route can
        contain any separator you might pick. Joining on "|" made
        ("a|b", "c", "d") and ("a", "b|c", "d") hash identically.

        Backslashes in `subject` are folded to "/" for hashing only, so that a
        rejection recorded on Windows suppresses the same finding on Linux CI.
        This deliberately conflates the two separators: subjects are
        predominantly paths, and a subject where "\\" and "/" mean different
        things is rare enough that colliding them beats splitting every finding
        in a cross-platform project down the middle. `subject` itself is left
        exactly as the lens gave it, for display.
        """
        raw = json.dumps([self.lens, self.rule_id, self.subject.replace("\\", "/")])
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass
class RunContext:
    """Everything a lens is allowed to know about the run."""

    project_root: Path
    state_root: Path
    # project_root-relative, already boundary-filtered. Not repo-relative: the
    # two differ for any monorepo, and `project_root / path` is what lenses open.
    files: tuple[Path, ...]
    tier: str
    lens_options: dict[str, Any]
    run_id: str
    skips: list[str] = field(default_factory=list)

    @property
    def options(self) -> dict[str, Any]:
        """Pack-specific options, from `lenses.<name>.options` in the config.

        Spine keys -- `enabled`, `autonomy`, `trust`, `only`, `severity_floor`
        -- stay at the top level of `lens_options` and are typed by
        `LensConfig`. Anything a pack invents lives in here instead, because
        `LensConfig` forbids extra keys (that is what makes a typo in a spine
        key fail loudly) and cannot possibly enumerate the vocabulary of a
        third-party lens installed through the entry point.

        Returns an empty mapping when the key is absent or the wrong shape, so
        a detector can always call `.get()` on it.
        """
        value = self.lens_options.get("options")
        return value if isinstance(value, dict) else {}

    def skip(self, reason: str) -> None:
        """Record work not done. A skip that is not reported is a bug."""
        self.skips.append(reason)


@runtime_checkable
class LensPack(Protocol):
    name: str
    max_autonomy: int

    def supports_tier(self, tier: str) -> bool: ...

    def run(self, ctx: RunContext) -> Iterator[Candidate]: ...
