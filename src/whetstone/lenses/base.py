"""The contract every lens pack implements.

A lens produces candidates and evidence. It never touches git, never publishes
to a sink, never writes to the store, and never decides its own autonomy.
Everything with consequences belongs to the spine.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

# Re-exported: `Severity` is shared with `config`, which must not import the
# lens layer. See whetstone/severity.py.
from ..errors import LensError
from ..severity import Severity as Severity
from ..severity import severity_at_least as severity_at_least

# A surrogate is the one thing a Python `str` can hold that UTF-8 cannot
# encode. `errors="surrogateescape"` produces them from bytes that were not
# valid UTF-8, and a JSON `\ud800` escape produces one without any byte ever
# having been malformed -- so model output, which M1a's candidates are made of,
# reaches here carrying them by the ordinary route rather than an exotic one.
#
# This is the home `detectors/deps.py` named for its own copy: "the right
# long-term home for this is `Evidence`/`Candidate` refusing unstorable text at
# construction, so every lens inherits it instead of each one remembering". That
# is now here, and deps.py imports it rather than keeping a second copy.
#
# `scope/resolver.py` keeps a DIFFERENT and deliberately narrower pattern,
# `[\udc80-\udcff]`, and is left alone: it is specifically about what
# surrogateescape leaves behind for a bad byte from git, which is that range and
# only that range. Merging the two would widen a check whose narrowness is
# argued in place.
_SURROGATE = re.compile("[\ud800-\udfff]")


def unstorable(value: object) -> bool:
    """True when *value* holds text UTF-8 cannot represent, at any depth.

    Shared with `detectors/deps.py`, which uses it on raw `pip-audit` output
    before a `Candidate` is built so it can report a SKIP naming the package
    rather than lose the whole run to a hard failure.

    Mappings are walked over KEYS as well as values. This is not paranoia about
    depth: `json.dumps` defaults to `ensure_ascii=True`, so a surrogate buried
    in `evidence.data` serialises to the ASCII text `\\udce9`, encodes cleanly,
    and stores without complaint -- then `json.loads` hands a real lone
    surrogate back to whatever renders it. Encoding the serialised JSON
    therefore cannot detect this and a structural walk is the only thing that
    can. deps.py only ever passes strings and lists of strings, so the extra
    arms change nothing there.
    """
    if isinstance(value, str):
        return _SURROGATE.search(value) is not None
    if isinstance(value, dict):
        return any(
            unstorable(key) or unstorable(item) for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(unstorable(item) for item in value)
    return False


def _require_text(owner: str, field_name: str, value: object) -> None:
    """Refuse anything that is not non-blank, storable text.

    `str` rather than "stringifiable" on purpose. Almost everything stringifies,
    which is exactly the defect: `rule_id=123` becomes "123" somewhere down the
    stack and stores cleanly under an identity no other run reproduces.

    And `str` is not sufficient either. A lone surrogate is a perfectly ordinary
    `str` that passes `isinstance` and `.strip()`, and then dies in sqlite with
    a `UnicodeEncodeError` -- which is NOT a `WhetstoneError`, so it escapes the
    CLI's `except WhetstoneError` and reaches the user as a bare traceback, from
    inside a transaction whose `runs` row already exists. That is precisely the
    failure `Evidence.__post_init__` says this validation exists to prevent, and
    it was reachable through the front door until this check existed.
    """
    if not isinstance(value, str):
        raise LensError(
            f"{owner}: {field_name} must be a string, not "
            f"{type(value).__name__} ({value!r}). Every lens field reaches "
            "sqlite; a non-string is stored under an identity nothing else "
            "will produce again."
        )
    if not value.strip():
        raise LensError(
            f"{owner}: {field_name} is empty. An empty subject is a finding "
            "about nothing, and an empty rule_id makes every finding from a "
            "lens the same finding."
        )
    if unstorable(value):
        raise LensError(
            f"{owner}: {field_name} contains text UTF-8 cannot encode "
            f"({ascii(value)}). sqlite would raise UnicodeEncodeError, which "
            "is not a WhetstoneError, so it would reach the user as a bare "
            "traceback from inside the run's own transaction."
        )


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


def lens_scope_declaration(pack: LensPack) -> tuple[LensScope, str | None]:
    """A pack's scope, plus the reason an invalid declaration was ignored.

    Not declaring a scope and declaring a broken one both end at `file`, and
    they are not the same event. A pack written before `scope` existed is
    behaving correctly; a pack declaring `scope = "projet"` has been silently
    overruled, and the boundaries advisory it should have produced is missing
    with nothing saying why. The runner records the reason so the second case
    is visible.
    """
    declared = getattr(pack, "scope", LensScope.file)
    try:
        return LensScope(declared), None
    except ValueError:
        return LensScope.file, (
            f"{getattr(pack, 'name', 'lens')}: declared scope {declared!r} is not "
            f"one of {', '.join(s.value for s in LensScope)}; the declaration was "
            "ignored and the lens was treated as file-scoped."
        )


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
    return lens_scope_declaration(pack)[0]


class EvidenceKind(StrEnum):
    metric = "metric"      # a measured value crossing a threshold
    repro = "repro"        # an executable artifact that fails before, passes after
    capture = "capture"    # a screenshot plus replayable navigation
    critique = "critique"  # a judgement with a cited heuristic -- never provable


@dataclass(frozen=True)
class Evidence:
    kind: EvidenceKind
    summary: str
    data: dict[str, Any]
    artifacts: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        """Validate at construction, for the reason argued on `Candidate`.

        `data` is probed for JSON-encodability here rather than left to
        `to_json`, which runs inside the store's transaction: a TypeError there
        aborts a run whose `runs` row already exists, three layers from the
        lens that caused it.
        """
        if not isinstance(self.kind, EvidenceKind):
            raise LensError(
                f"evidence kind must be an EvidenceKind, not "
                f"{type(self.kind).__name__} ({self.kind!r}). Valid kinds: "
                f"{', '.join(k.value for k in EvidenceKind)}."
            )
        _require_text("evidence", "summary", self.summary)
        if not isinstance(self.data, dict):
            raise LensError(
                f"evidence data must be a mapping, not "
                f"{type(self.data).__name__} ({self.data!r})."
            )
        # Structural, and BEFORE the dumps probe, because dumps cannot see it:
        # `ensure_ascii=True` renders a surrogate as the ASCII text `\udce9`,
        # which encodes and stores cleanly, and `json.loads` then hands a real
        # lone surrogate back to whatever renders the finding.
        if unstorable(self.data):
            raise LensError(
                "evidence data contains text UTF-8 cannot encode. It survives "
                "json.dumps as an ASCII escape and comes back a lone surrogate "
                "on the way out, so the failure lands on a later reader rather "
                "than on the lens that produced it."
            )
        try:
            # `allow_nan=False` because the default emits a bare `NaN`, which
            # every JSON parser outside Python rejects: `to_json` would produce
            # a document the store accepts and no other consumer can read.
            json.dumps(self.data, sort_keys=True, allow_nan=False)
        except (TypeError, ValueError) as exc:
            raise LensError(
                f"evidence data is not storable as JSON ({exc}). It is stored "
                "as UTF-8 JSON, so a value that cannot be encoded fails inside "
                "the store's transaction rather than here."
            ) from None
        # `isinstance(artifacts, str)` first: a string is a sequence, so
        # `tuple("a.txt")` turns one path into eight single-character ones and
        # every per-item check below then passes.
        if isinstance(self.artifacts, str) or not isinstance(
            self.artifacts, (tuple, list)
        ):
            raise LensError(
                f"evidence artifacts must be a tuple or list of paths, not "
                f"{type(self.artifacts).__name__} ({self.artifacts!r})."
            )
        for index, artifact in enumerate(self.artifacts):
            if not isinstance(artifact, str):
                raise LensError(
                    f"evidence artifacts[{index}] must be a string, not "
                    f"{type(artifact).__name__} ({artifact!r})."
                )

    def to_json(self) -> str:
        """Deterministic JSON -- key order must not affect stored bytes.

        `allow_nan=False` matches the construction-time probe. `data` is a
        mutable dict inside a frozen dataclass, so a lens CAN put a NaN or a
        surrogate in after construction and reinstate the failure the probe
        exists to prevent; freezing it would mean a deep copy on every
        candidate. Raising here rather than emitting a bare `NaN` keeps the
        worst case a loud failure instead of a stored document no JSON parser
        outside Python will read.
        """
        return json.dumps(
            {
                "kind": str(self.kind),
                "summary": self.summary,
                "data": self.data,
                "artifacts": list(self.artifacts),
            },
            sort_keys=True,
            allow_nan=False,
        )


@dataclass(frozen=True)
class Candidate:
    """What a lens produces. The spine turns it into a Finding."""

    lens: str
    rule_id: str
    subject: str  # file path, package name, route -- what this is *about*
    title: str
    detail: str
    severity: Severity
    evidence: Evidence

    def __post_init__(self) -> None:
        """Validate every leaf at construction, not per detector.

        Issues #14 and #9 are one defect seen twice: a lens hands the spine a
        field the store cannot use. It is validated HERE rather than where the
        external data enters because a lens pack arrives through an entry point
        -- third-party code that never saw this file -- and because M1a's
        candidates come from a model, where a wrong-typed field is the expected
        case rather than an exotic one.

        The three `dedupe_key` components are the sharpest of them: the key
        JSON-encodes `lens`, `rule_id` and `subject`, so a non-string does not
        raise, it changes the hash. The finding stores cleanly under an identity
        no later run reproduces, so it can never be deduped and a rejection
        recorded against it can never suppress it.

        `severity` must be the enum rather than anything that stringifies. That
        is issue #9: `upsert` does `str(candidate.severity)`, so `None` was
        stored as the string 'None' -- the row is queued, `list_findings` ranks
        it into the ELSE bucket, and nothing says it is wrong. Every other None
        from a lens hits a NOT NULL constraint; this was the one that became
        plausible-looking data. 'HIGH' and 'sev-1' land in the same bucket for
        the same reason, so a plain string is refused too and a lens that reads
        a severity out of model output has to map it explicitly.
        """
        owner = self.lens if isinstance(self.lens, str) and self.lens.strip() else "lens"
        for field_name in ("lens", "rule_id", "subject", "title", "detail"):
            _require_text(owner, field_name, getattr(self, field_name))
        if not isinstance(self.severity, Severity):
            raise LensError(
                f"{owner}: severity must be a Severity, not "
                f"{type(self.severity).__name__} ({self.severity!r}). Valid "
                f"severities: {', '.join(s.value for s in Severity)}. The store "
                "writes str(severity), so anything else is recorded as "
                "plausible-looking text and ranked below 'medium'."
            )
        if not isinstance(self.evidence, Evidence):
            raise LensError(
                f"{owner}: evidence must be an Evidence, not "
                f"{type(self.evidence).__name__} ({self.evidence!r}). The store "
                "calls evidence.to_json(); anything else raises AttributeError "
                "inside the transaction."
            )

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
    # Private, with `skip()` as the only writer and `skips` a read-only view.
    #
    # This was a plain public list, so a lens could clear its own trail
    # (`ctx.skips.clear()`, `ctx.skips[:] = []`), rebind the attribute
    # (`ctx.skips = []`), or forge an entry that no skip() call produced. The
    # blast radius is self-only -- every lens gets a freshly constructed
    # RunContext, which is deliberate; sharing the run's list into every context
    # was tried, let one lens erase every other lens's trail while the run still
    # reported status='complete', and was reverted. What remained was that a
    # lens's own skip record is not trustworthy, which matters little for
    # first-party code and a lot once lens packs are third-party.
    #
    # RESIDUAL, stated rather than implied: Python has no enforceable privacy,
    # so `ctx._skips` is still reachable by a lens that goes looking for it.
    # What this closes is every path that does not have to say so -- the public
    # name is now a snapshot, and reaching past it is a deliberate act that
    # shows up in a diff. A lens pack running in-process can always be trusted
    # exactly as far as the process is; real isolation is a subprocess boundary,
    # which is what M1a's provider gives the model-driven stages.
    _skips: list[str] = field(default_factory=list, repr=False)

    @property
    def skips(self) -> tuple[str, ...]:
        """The skips recorded so far, as a snapshot.

        A tuple rather than the list itself: returning the list would be the
        same mutable handle wearing a property. A tuple copy also means a view
        taken before a later `skip()` cannot be used to reach back into it.
        """
        return tuple(self._skips)

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
        """Record work not done. A skip that is not reported is a bug.

        The only writer. *reason* must be non-blank text because a skip is
        rendered straight into the console and the HTML report, and the whole
        point of a skip is that a human reads it.
        """
        _require_text("skip", "reason", reason)
        self._skips.append(reason)


@runtime_checkable
class LensPack(Protocol):
    name: str
    max_autonomy: int

    def supports_tier(self, tier: str) -> bool: ...

    def run(self, ctx: RunContext) -> Iterator[Candidate]: ...
