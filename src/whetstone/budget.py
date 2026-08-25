"""The run-level cost ceiling, and the ledger the estimator will be fit to.

WHY RUN-LEVEL AND NOT PER-STAGE, WHICH IS A MEASUREMENT RATHER THAN A TASTE.
The CLI's own `--max-budget-usd` below about $0.35 makes a stage a guaranteed
no-op: the budget is exhausted before the first tool call, so the stage returns
having read nothing. A per-stage ceiling small enough to be a useful bound is
therefore small enough to break the stage it bounds. `StageRequest.max_budget_usd`
stays None everywhere, and the ceiling lives here instead, counting what came
back and stopping the run between stages.

WHAT spend() READS. `Usage.total_tokens` and `Usage.cost_usd`, never
`input_tokens`. A measured trivial call reported `input_tokens: 4` alongside
`cache_creation_input_tokens: 41036` -- a budget reading the first field alone
under-reports by four orders of magnitude, and it under-reports SILENTLY, which
is the shape this project keeps paying for.

AN UNMEASURED COST IS NOT A FREE ONE. `cost_usd=None` means the provider could
not measure the call, and adding zero for it is the same silent under-count one
level down. It is counted in `unmeasured_calls` so the caller can say that the
ceiling was enforced against a number known to be short.

THE ENFORCEMENT POINT IS `BudgetedProvider`, not a check the caller remembers.
A ceiling checked only between candidates cannot stop a hunt that runs one
stage per angle inside a single call, so the wrapper sits where every stage
already goes: `Provider.run_stage`. A stage refused by the ceiling comes back as
an ordinary failed `StageResult`, which every stage in this package already
turns into a skip naming the reason.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .provider.base import Provider, StageRequest, StageResult, Usage


@dataclass(frozen=True)
class StageCost:
    """What one stage cost, kept so the estimator can be fit to real numbers.

    `subject` is the candidate the stage was about, or the stage's own name for
    work that is not about one candidate. Recorded because the predecessor's
    estimates were 4-17x low and the reason was never recoverable: cost was
    summed and the per-stage shape thrown away.
    """

    stage: str
    subject: str
    cost_usd: float | None
    tokens: int
    wall_seconds: float
    source: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "subject": self.subject,
            "cost_usd": self.cost_usd,
            "tokens": self.tokens,
            "wall_seconds": self.wall_seconds,
            "source": self.source,
        }


@dataclass
class Budget:
    """A run's cost ceiling and everything spent under it.

    Both ceilings are optional and both are checked with `>=`: a ceiling reached
    to the cent is reached. `None` means unbounded; `0.0` means a real ceiling of
    nothing, which is exhausted from the start. Those are different, and the
    `if not ceiling` spelling collapses them -- `usd_per_run: 0` would then read
    as "no ceiling at all", which is the exact opposite of what it says.
    """

    ceiling_usd: float | None = None
    ceiling_calls: int | None = None
    spent_usd: float = 0.0
    tokens: int = 0
    calls: int = 0
    # Calls whose cost the provider could not measure. See the module docstring:
    # these are NOT free, they are unknown, and a ceiling enforced against them
    # is enforced against a number that is short by an unknown amount.
    unmeasured_calls: int = 0
    _ledger: list[StageCost] = field(default_factory=list, repr=False)

    @property
    def ledger(self) -> tuple[StageCost, ...]:
        """A snapshot. Handing back the list would let a caller edit the record
        of what a run cost, which is the one number nobody should be able to
        revise after the fact."""
        return tuple(self._ledger)

    def spend(self, usage: Usage, *, stage: str = "", subject: str = "") -> None:
        """Record what one stage cost. See the module docstring for the fields."""
        self.calls += 1
        self.tokens += usage.total_tokens
        if usage.cost_usd is None:
            self.unmeasured_calls += 1
        else:
            self.spent_usd += usage.cost_usd
        self._ledger.append(
            StageCost(
                stage=stage,
                subject=subject,
                cost_usd=usage.cost_usd,
                tokens=usage.total_tokens,
                wall_seconds=usage.wall_seconds,
                source=usage.source,
            )
        )

    def remaining(self) -> float | None:
        """Dollars left, or None when there is no dollar ceiling.

        Floored at zero: an overspend is real and is visible in `spent_usd`, but
        a negative "remaining" reads as a credit in the one sentence a user sees
        when the run stops.
        """
        if self.ceiling_usd is None:
            return None
        return max(0.0, self.ceiling_usd - self.spent_usd)

    def remaining_calls(self) -> int | None:
        if self.ceiling_calls is None:
            return None
        return max(0, self.ceiling_calls - self.calls)

    def exhausted(self) -> bool:
        return self.reason() is not None

    def reason(self) -> str | None:
        """Why nothing further may run, or None. The sentence a user reads.

        Returned rather than raised: the caller has to record what it did not
        reach before it stops, and an exception would land in whatever frame was
        mid-stage instead.
        """
        if self.ceiling_usd is not None and self.spent_usd >= self.ceiling_usd:
            return (
                f"the run budget is exhausted: ${self.spent_usd:.4f} of the "
                f"${self.ceiling_usd:.4f} ceiling (`budget.ceiling.usd_per_run`) "
                f"has been spent."
            )
        if self.ceiling_calls is not None and self.calls >= self.ceiling_calls:
            return (
                f"the run budget is exhausted: {self.calls} of {self.ceiling_calls} "
                f"permitted model calls have been made."
            )
        return None


class BudgetedProvider:
    """A `Provider` that spends from a `Budget` and stops when it is empty.

    Wraps rather than subclasses, because providers arrive through an entry
    point and there is no class to inherit from. `name` is the wrapped
    provider's own: every message that names a provider should name the one
    doing the work, not the accountant in front of it.
    """

    def __init__(self, inner: Provider, budget: Budget, subject: str = "") -> None:
        self._inner = inner
        self.budget = budget
        self.name = inner.name
        # Set by the caller before each candidate so the ledger says what the
        # money was spent on. A plain attribute rather than a parameter on
        # `run_stage`, which is the Provider protocol's signature and not ours
        # to widen.
        self.subject = subject

    def run_stage(self, request: StageRequest) -> StageResult:
        reason = self.budget.reason()
        if reason is not None:
            # A failed StageResult with a reason, which is the shape every stage
            # in the code-defects pack already turns into a skip. Deliberately
            # NOT an exception: a stage refused by the ceiling is work not done,
            # and work not done has to reach the user as a reason rather than as
            # a traceback.
            return StageResult(
                ok=False,
                data=None,
                raw="",
                usage=Usage(),
                error=f"{reason} Nothing further was run.",
            )
        result = self._inner.run_stage(request)
        self.budget.spend(result.usage, stage=request.stage, subject=self.subject)
        return result


# Characters a lens name may contribute to a filename. Everything else is
# replaced, because the lens name reaches this path from the registry and M5
# opens the registry to third parties -- a name containing a separator would
# otherwise write the cost record somewhere other than the costs directory.
_SAFE_LENS_NAME = re.compile(r"[^A-Za-z0-9._-]")

# The separator between a sanitised name and its disambiguating digest.
# Deliberately a character `_SAFE_LENS_NAME` REPLACES, so it cannot occur
# in the sanitised part and the digest namespace is unreachable by any
# lens name. See `_cost_filename`.
_DIGEST_MARKER = "~"


def _cost_filename(run_id: str, lens: str) -> str:
    """`<run_id>.<lens>.json`, with the lens name made safe for a filename.

    KEYED BY LENS AS WELL AS RUN, which the first version was not. It wrote
    `<run_id>.json` while carrying a `"lens"` field inside -- a record whose
    own contents say more than one lens was expected, in a filename that can
    only hold one. Nothing exposed it because exactly one lens wrote a record;
    the second one to do so would have silently overwritten the first, and the
    lost half is money that was spent.

    A sanitised name is disambiguated with a short digest of the original, so
    two lenses that sanitise to the same string still get two files rather than
    quietly becoming one.
    """
    safe = _SAFE_LENS_NAME.sub("_", lens)
    # CASE AS WELL AS CHARACTERS. Windows and the default macOS filesystem fold
    # case, so `Foo` and `foo` both survive sanitisation unchanged, produce
    # `run-1.Foo.json` and `run-1.foo.json`, and land on ONE file -- the second
    # lens silently overwriting the first, which is the collision this function
    # exists to prevent, one level deeper than the character check.
    if safe != lens or safe != safe.lower():
        # `~`, NOT `-`. `_SAFE_LENS_NAME` permits `-`, so a hyphen-joined
        # digest lives in the same namespace as an ordinary lens name and the
        # collision comes back one level up: `Foo` sanitises to `Foo-<d>`, and
        # a lens literally named `foo-<d>` stays `foo-<d>`. Those fold to one
        # file on Windows and macOS, which is the exact overwrite this function
        # exists to prevent.
        #
        # `~` is outside the permitted class, so it can only ever appear here
        # -- no lens name can produce one, because any `~` in a name is
        # replaced before this line runs.
        digest = hashlib.sha256(lens.encode("utf-8")).hexdigest()[:8]
        safe = f"{safe}{_DIGEST_MARKER}{digest}"
    return f"{run_id}.{safe}.json"


def write_cost_record(
    *,
    state_root: Path,
    run_id: str,
    lens: str,
    tier: str,
    budget: Budget,
    on_skip: Callable[[str], None],
) -> None:
    """Per-stage cost against this run, for the estimator to be fit to.

    ONE WRITER, and that is why this lives here rather than on a lens pack.
    `code-defects` had this as a private method and `rendered-ui` -- which
    builds a `Budget`, wraps its provider in a `BudgetedProvider` and spends
    real money through it -- had no equivalent at all. Its entire spend was
    unrecorded, so the cost surface under-reported by one whole lens without
    saying anything, which is the same silent under-count `Budget.spend`
    already guards against one level down.

    A FILE UNDER THE STATE DIRECTORY RATHER THAN A TABLE. `store/db.py` refuses
    to open a database stamped with a different `user_version` and states there
    is no migration path, so a new table would refuse every database written by
    an earlier build.

    WRITTEN EVEN WHEN NOTHING WAS SPENT, and the earlier version's reasoning
    for skipping it was exactly backwards. It said "an empty record would read
    as a run that cost nothing rather than one that never called a model" --
    but a record carrying `calls: 0` and `stages: []` says precisely "this lens
    ran and made no billable call", which is a fact. ABSENCE is the ambiguous
    thing: it collapses "the lens was not configured", "the lens declined
    before it built a budget" and "the lens ran and spent nothing" into one
    silence, and a cost surface then cannot tell $0.00 from unmeasured. Writing
    it always makes absence mean one checkable thing -- no budget was ever
    constructed -- and every path that ends that way already reports a skip.

    `on_skip` rather than a `RunContext`: the only thing this needs is the
    channel a lens has to the user, and taking the whole context would make
    the one function that must work from every pack depend on the shape of the
    caller's.
    """
    record = {
        "run_id": run_id,
        "lens": lens,
        "tier": tier,
        "ceiling_usd": budget.ceiling_usd,
        "spent_usd": budget.spent_usd,
        "tokens": budget.tokens,
        "calls": budget.calls,
        "unmeasured_calls": budget.unmeasured_calls,
        "stages": [entry.as_dict() for entry in budget.ledger],
    }
    path = state_root / "costs" / _cost_filename(run_id, lens)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(record, indent=2), encoding="utf-8")
    except OSError as exc:
        on_skip(
            f"{lens}: this run's per-stage cost could not be written to "
            f"{path.name} ({exc}), so ${budget.spent_usd:.4f} across "
            f"{budget.calls} model call(s) was spent and not recorded."
        )
    if budget.unmeasured_calls:
        on_skip(
            f"{lens}: {budget.unmeasured_calls} of {budget.calls} model "
            f"call(s) reported no cost at all, so the ceiling was enforced "
            f"against a total known to be short."
        )
