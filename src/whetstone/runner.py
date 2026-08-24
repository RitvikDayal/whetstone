"""Orchestrates one run. Owns skip reporting; a skip that is not reported is a bug."""

from __future__ import annotations

import contextlib
import json
import sqlite3
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .config.model import BoundariesConfig, LensConfig, WhetstoneConfig
from .errors import WhetstoneError
from .lenses.base import (
    LensPack,
    LensRuntime,
    LensScope,
    RunContext,
    lens_scope_declaration,
)
from .lenses.registry import get_lens
from .scope.resolver import resolve_files
from .severity import severity_at_least
from .store.findings import upsert

# A progress sink. Takes one JSON-safe dict per event; returns nothing and
# is never consulted about what to do next -- see `_emit`.
EventSink = Callable[[dict[str, Any]], None]


@dataclass
class RunResult:
    """What one run did, in the form every surface renders.

    `status` is required rather than defaulted. It is the difference between a
    run that finished and one that died halfway, and it was previously not
    carried here at all: `get_last_run` selected the row and dropped the
    column, so `report` rendered an interrupted run as a clean document. A
    default would let a construction site stay silent about the one field that
    exists to stop that, so every caller says which it is.

    `lens_count` is how many lenses this run actually executed. None means the
    number is unknown, not zero -- `get_last_run` reconstructs from the `runs`
    table, which has no such column, and claiming 0 there would assert that a
    real run examined nothing. Only a live `execute_run` sets it.
    """

    run_id: str
    tier: str
    file_count: int
    status: str
    new: int = 0
    seen: int = 0
    skips: list[str] = field(default_factory=list)
    lens_count: int | None = None

    @property
    def finished(self) -> bool:
        return self.status == "complete"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def get_last_run(conn: sqlite3.Connection) -> RunResult | None:
    """Reconstruct the most recently STARTED run, skips included, or None.

    None means no run has ever happened here -- a distinct state from a run
    that happened and recorded no skips, and callers (the HTML report, in
    particular) must be able to tell the two apart rather than rendering
    both as silence.

    Ordered by started_at, not finished_at or status: a run that failed
    mid-way is exactly the one whose skips and partial file_count matter
    most to whoever reads a report generated afterward, so filtering out
    anything but status='complete' would hide the run most worth surfacing.

    Tied by SQLite's implicit rowid as a second key. `started_at` is an
    ISO-8601 string from `datetime.now(UTC)`, and two `execute_run` calls
    close enough together landed on the identical string in practice --
    measured directly in this test suite, not a theoretical worry -- which
    made plain `ORDER BY started_at DESC LIMIT 1` pick whichever row SQLite
    felt like on a tie, and it was consistently the OLDER one. `runs.id` is
    TEXT, not the rowid, but rowid still exists and still increases with
    insertion order, so it recovers the real ordering a tied string cannot.

    `new`/`seen` are not columns on `runs` (see the schema in store/db.py) --
    they are derived here from `findings.first_seen_run`/`last_seen_run`
    against this run's own id, the same counts `execute_run` itself produced
    live while upserting. Defaulting them to 0 instead would silently render
    a real run's history as if nothing happened, which is the exact kind of
    quiet loss this module exists to forbid.

    `status` is carried for the same reason and used to be dropped. This
    function did `SELECT *` and then read `tier`, `file_count` and
    `skipped_json` out of the row while leaving the one column that says
    whether the run finished behind, so a Ctrl-C during a slow pip-audit --
    status='failed', zero findings, no skip recorded, because an interrupt
    records none -- reached `report/html.py` as a run indistinguishable from a
    clean one and rendered "No open findings." The justification above for
    including failed runs rests on their skips mattering most, and an
    interrupted run has no skips, so the status is the only evidence that
    exists in exactly the case the docstring was written for.
    """
    row = conn.execute(
        "SELECT * FROM runs ORDER BY started_at DESC, rowid DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    run_id = row["id"]
    new = conn.execute(
        "SELECT COUNT(*) FROM findings WHERE first_seen_run = ?", (run_id,)
    ).fetchone()[0]
    seen = conn.execute(
        "SELECT COUNT(*) FROM findings WHERE last_seen_run = ? AND first_seen_run != ?",
        (run_id, run_id),
    ).fetchone()[0]
    return RunResult(
        run_id=run_id,
        tier=row["tier"],
        file_count=row["file_count"],
        status=row["status"],
        new=new,
        seen=seen,
        skips=json.loads(row["skipped_json"]),
    )


# M0 ships one sink: the built-in store, which every command already reads.
# Anything else declared in config is not silently dropped.
_IMPLEMENTED_SINKS = frozenset({"dashboard"})


_DEFAULT_INCLUDE = BoundariesConfig().include


def _boundaries_are_narrowed(boundaries: BoundariesConfig) -> bool:
    """True when include/exclude actually restrict the run.

    An untouched `boundaries` block excludes nothing, so telling the user that
    a project-scoped lens ignored it would be a line that is true, useless, and
    printed on every single run. Those are the lines that teach people to stop
    reading the skip list.
    """
    return bool(boundaries.exclude) or boundaries.include != _DEFAULT_INCLUDE


def _rendered(exc: BaseException) -> str:
    """*exc*'s message, or why it has none. Never raises.

    `str(exc)` RUNS A THIRD-PARTY `__str__` -- an ordinary override, and the
    one thing a pack is invited to define about its own exception. It can
    raise, and a `__str__` returning a non-str makes `str()` itself raise
    `TypeError`. Either lands inside the `except` block that exists to stop a
    pack ending the run, where there is no handler left above it and no `runs`
    row yet: the renderer of the safety message becomes the unsafe thing.

    HOW FAR THIS GOES, AND WHERE IT STOPS. There is no second attempt at the
    message: `repr()` is not a safer fallback, because `__repr__` is equally a
    pack's to override, so a failure yields a fixed sentence instead. The TYPE
    NAME is read by the callers rather than here, as `type(exc).__name__` --
    `type()` returns the real type object, so a `__class__` property cannot lie
    about it, and `__name__` on a class runs no pack code. Making THAT raise
    needs a custom metaclass on an exception class, and a pack prepared to go
    that far could equally just hang in `configure`, which no amount of
    rendering care would survive. The line is what a pack is invited to
    define, not what it can subvert with a metaclass.
    """
    try:
        text = str(exc)
    except Exception:  # noqa: BLE001 - rendering a reason must not end the run
        return "its message could not be rendered"
    # Stripped, so a whitespace-only message is the same empty parenthetical
    # that `str(exc) or ...` was fixed to prevent, by a slower route.
    return text.strip() or "no message"


def _lens_runtime(cfg: WhetstoneConfig) -> LensRuntime:
    """The narrowed record `configure()` receives instead of the whole config.

    Built here rather than in `lenses/` because the runner is the only layer
    that holds a `WhetstoneConfig`, and this is exactly the seam that keeps it
    that way: `state_dir` never crosses it.
    """
    return LensRuntime(
        provider_name=cfg.model.provider,
        test_command=cfg.environment.commands.test,
        ceiling_usd=cfg.budget.ceiling.usd_per_run,
        calls_per_day=cfg.budget.ceiling.calls_per_day,
    )


def _nothing_ran_reason(cfg: WhetstoneConfig) -> str:
    """Why an empty plan happened, worded so it cannot be read as a clean bill.

    Two distinct causes, and telling a user "no lens is configured" when they
    configured three and disabled them all sends them to the wrong line of the
    file. Both endings say the same thing about the RESULT, because the result
    is identical and it is not evidence of anything.
    """
    if not cfg.lenses:
        cause = (
            "whetstone.yaml declares no lenses at all -- the `lenses:` key is "
            "absent or empty, which defaults to an empty map. Add one (for "
            "example `lenses: {hygiene: {enabled: true}}`) or re-run "
            "`whetstone init`."
        )
    else:
        cause = (
            "every lens declared in whetstone.yaml was skipped; the reasons are "
            "listed alongside this line."
        )
    return (
        f"NO LENS RAN: {cause} Nothing was examined, so this run is not "
        "evidence that the project is clean -- it is evidence that nothing "
        "was checked."
    )


def _report_unsupported_sinks(cfg: WhetstoneConfig, result: RunResult) -> None:
    for sink in cfg.sinks:
        if sink.kind not in _IMPLEMENTED_SINKS:
            result.skips.append(
                f"sink '{sink.kind}': not implemented in this version; findings "
                "were NOT published there. Only the built-in local sink is "
                "available."
            )


def _emit(on_event: EventSink | None, **event: Any) -> None:
    """Hand one progress event to *on_event*, and never let it end the run.

    A run that dies because the thing WATCHING it raised has been destroyed by
    its own instrumentation. The callback belongs to a surface -- today the
    control plane's SSE stream -- and a surface is not allowed to be load
    bearing: every event here also lands in the store or in `result.skips`, so
    a sink that raises loses liveness and no information.
    """
    if on_event is None:
        return
    # `Exception`, not `BaseException`: a Ctrl-C raised while the sink is
    # running is the user ending the run, and swallowing it here would make the
    # run unstoppable from the one place a user can reach it. Same distinction
    # the lens loop below already draws.
    with contextlib.suppress(Exception):
        on_event(event)


def execute_run(
    conn: sqlite3.Connection,
    cfg: WhetstoneConfig,
    project_root: Path,
    state_root: Path,
    *,
    tier: str,
    changed_only: bool,
    on_event: EventSink | None = None,
) -> RunResult:
    """Run every enabled lens. `on_event` is optional live progress.

    ON_EVENT IS LENS-AGNOSTIC, and that is what keeps the M2 abstraction gate
    green. A progress event is about a RUN -- which lens started, what it
    found, what it skipped -- and runs are the spine's own concern. Nothing
    here learns what a lens does, only that one is running.

    THE STREAM IS A CONVENIENCE, NEVER THE RECORD. Every event below restates
    something that is also written to the `runs` or `findings` tables or to
    `result.skips`. A client that misses every event and refreshes sees the
    same state, because the state was never only in the stream.
    """
    run_id = f"run-{uuid.uuid4().hex[:10]}"
    started = _now()

    # Which lenses will actually run is decided BEFORE anything else, because
    # it decides whether the files even need resolving. See below.
    #
    # This puts lens lookup ahead of the `runs` INSERT, so a lens plugin that
    # fails to load now propagates before any row exists, the same way
    # resolve_files does. That is the intended shape: neither failure leaves a
    # run to report against, and both mean the run never started.
    plan: list[tuple[str, LensConfig, LensPack]] = []
    # Resolved once per pack rather than re-derived at each use: an invalid
    # declaration must be reported exactly once, not once per question asked
    # about it. Keyed by lens name, which config guarantees is unique.
    scopes: dict[str, LensScope] = {}
    skips: list[str] = []
    # Built ONCE, and outside the try below. It is loop-invariant, and keeping
    # it out of the `except` means a defect in Whetstone's own narrowing cannot
    # be absorbed into a per-lens skip and reported as a misbehaving pack.
    runtime = _lens_runtime(cfg)
    for name, lens_cfg in cfg.lenses.items():
        if not lens_cfg.enabled:
            skips.append(f"{name}: disabled in config; not run.")
            continue

        pack = get_lens(name)
        if pack is None:
            skips.append(
                f"{name}: not installed; no lens pack with that name is "
                "registered. Not run."
            )
            continue

        if not pack.supports_tier(tier):
            skips.append(
                f"{name}: not available at tier '{tier}'; not run. "
                "Use a higher tier to include it."
            )
            continue

        # A pack that needs the run's config gets it here and NOWHERE else.
        # `RunContext` is deliberately "everything a lens is allowed to know"
        # and does not carry the config, so a lens needing the declared test
        # command or the cost ceiling would otherwise have to re-load
        # whetstone.yaml for itself -- duplicating the loader and walking
        # around the boundary that keeps a third-party pack out of a project's
        # resolved secrets.
        #
        # A `LensRuntime` rather than `cfg`, which is that same sentence being
        # true rather than merely written down: `WhetstoneConfig.state_dir` is
        # a `SecretStr` the loader has already resolved, and handing the whole
        # object over put `get_secret_value()` one attribute away from every
        # in-process entry-point pack. See `LensRuntime` for what this does and
        # does not claim to be.
        #
        # Optional, and read with getattr for the reason `lens_scope` is not a
        # protocol member either: `runtime_checkable` isinstance() checks every
        # attribute, so requiring `configure` would make `register()` reject
        # every pack written before this existed, including installed
        # third-party ones.
        #
        # The RETURN VALUE is used rather than mutating in place, because the
        # registry hands out one instance for the life of the process and
        # configuring that object would leak one project's settings into the
        # next run in the same process.
        configure = getattr(pack, "configure", None)
        if callable(configure):
            try:
                configured = configure(runtime)
            except WhetstoneError as exc:
                # `_rendered` here too. `LensError` is importable, so a pack
                # may subclass it and override `__str__`; being Whetstone's own
                # base class does not make the INSTANCE ours.
                skips.append(
                    f"{name}: could not be configured for this run "
                    f"({_rendered(exc)}); not run."
                )
                continue
            except Exception as exc:  # noqa: BLE001 - a pack must not end the run
                # THE SAME BLAST RADIUS AS THE RETURN-VALUE CHECK BELOW, and
                # for the same reason. `configure` is third-party code reached
                # through an entry point, and anything it raises that is not a
                # `WhetstoneError` -- an `AttributeError` on a `LensRuntime`
                # field it expected to be a config, a `KeyError`, a `TypeError`
                # -- escapes `execute_run` before the `runs` INSERT: no run row,
                # every other lens abandoned, and nothing anywhere saying which
                # pack did it.
                #
                # DELIBERATELY NOT THE SHAPE `registry._load_plugins` USES,
                # which re-raises. That failure is a plugin that could not LOAD,
                # which silently shrinks the registry for every lens and every
                # future call; this one is scoped to a lens that exists, is
                # named in the skip, and whose absence is visible in
                # `lens_count`. Loud and fatal there, loud and contained here.
                #
                # `Exception`, not `BaseException`: a Ctrl-C or a SystemExit
                # during configuration is the user or the process ending the
                # run, and turning either into a skip would swallow it.
                # `_rendered(exc)`, NOT `exc` and not `str(exc)`. An exception
                # instance is always truthy -- `BaseException` defines neither
                # `__bool__` nor `__len__` -- so `exc or ...` never reached its
                # fallback and a bare `raise ValueError` rendered as `raised
                # ValueError ()`. And `str(exc)` runs the pack's own `__str__`,
                # which can raise from inside this handler. See `_rendered`.
                skips.append(
                    f"{name}: its `configure` hook raised "
                    f"{type(exc).__name__} ({_rendered(exc)}), which "
                    "is not a Whetstone error. This lens was NOT run; the rest "
                    "of the run continued."
                )
                continue
            # CHECKED, NOT TRUSTED. `configure` is an optional hook on code
            # that arrives through an entry point, and the in-place spelling
            # its name invites -- `self.test_command = ...` with no return --
            # yields None. That would reach `lens_scope_declaration` as None
            # and raise `AttributeError`, which is not a `WhetstoneError`, so
            # it escapes `execute_run` BEFORE the `runs` row is inserted: no
            # run to report against, every other lens abandoned, one
            # third-party pack taking the whole run with it. A per-lens skip
            # is the blast radius the rest of this loop already uses.
            if not isinstance(configured, LensPack):
                skips.append(
                    f"{name}: its `configure` hook returned "
                    f"{type(configured).__name__} rather than a lens pack, so "
                    "this lens was NOT run. `configure` must return the "
                    "configured copy of the pack."
                )
                continue
            pack = configured

        scope, scope_reason = lens_scope_declaration(pack)
        if scope_reason is not None:
            skips.append(scope_reason)
        scopes[name] = scope
        plan.append((name, lens_cfg, pack))

    file_scoped = [name for name, _, _ in plan if scopes[name] is LensScope.file]
    if file_scoped:
        # Raises GitError on no merge base or undecodable tracked paths inside
        # the boundaries. Deliberately left to propagate rather than swallowed
        # into a skip: no `runs` row exists yet at this point (the INSERT below
        # hasn't happened), there is no file count and no scope to report a
        # partial run against, and resolve_files itself treats both cases as
        # hard stops for the same reason this whole module exists -- a scan
        # that quietly examined less than it claims is worse than one that
        # refuses to start. The caller (CLI) turns this into a clean message.
        files = resolve_files(
            project_root,
            cfg.boundaries,
            changed_only=changed_only,
            base_branch=cfg.project.forge.base_branch,
        )
    else:
        # Nothing in this run reads ctx.files, so resolving them buys a
        # file_count and nothing else -- and it used to cost the whole run: a
        # non-git project died in resolve_files before any lens started, even
        # though the only lens shipped today needs no files at all.
        files = ()
        if plan:
            skips.append(
                "boundaries: file resolution was not performed because no "
                f"enabled lens is file-scoped ({', '.join(n for n, _, _ in plan)}). "
                "file_count is 0 for that reason, not because the project is "
                "empty."
            )
        else:
            # The `if plan:` above used to be the ONLY line here, which
            # suppressed the skip exactly when nothing ran at all: a
            # whetstone.yaml with no `lenses:` key defaults to an empty map
            # (config/model.py), so the plan was empty, no lens was iterated,
            # no skip was recorded, and `run` printed "0 new, 0 already known"
            # with no warning. Against a project pinning a known-vulnerable
            # dependency at 5% coverage that same directory produced 24
            # findings once a lens was declared. An empty plan is the loudest
            # thing a run can have to say, not the quietest.
            skips.append(_nothing_ran_reason(cfg))

    if _boundaries_are_narrowed(cfg.boundaries):
        for name, _, _ in plan:
            if scopes[name] is LensScope.project:
                skips.append(
                    f"{name}: project-scoped. It reads fixed project artifacts "
                    "and never consults boundaries.include/exclude, so those "
                    "patterns did NOT narrow what it examined."
                )

    # Pessimistic from the moment it exists, for the reason spelled out at the
    # `try` below: `complete` is set by the one normal exit, never cleared by
    # an `except` that BaseException walks straight past.
    result = RunResult(
        run_id=run_id,
        tier=tier,
        file_count=len(files),
        status="failed",
        skips=skips,
        lens_count=len(plan),
    )

    conn.execute(
        "INSERT INTO runs (id, tier, scope_mode, file_count, started_at, status) "
        "VALUES (?, ?, ?, ?, ?, 'running')",
        (
            run_id,
            tier,
            "changed" if changed_only else "full",
            len(files),
            started,
        ),
    )

    # AFTER the row exists. An event announcing a run that no surface can then
    # look up is a promise the store cannot keep.
    _emit(
        on_event,
        kind="run_started",
        run_id=run_id,
        tier=tier,
        file_count=len(files),
        lens_count=len(plan),
        lenses=[name for name, _, _ in plan],
        skips=list(skips),
    )

    # Once the `runs` row exists, every exit path -- including a lens raising
    # mid-iteration -- must close it out with finished_at and a terminal
    # status. A row stuck at status='running' forever is its own kind of
    # silent skip: nothing ever tells the user the run didn't finish.
    #
    # Pessimistic by default, and `complete` is set by the one normal exit
    # below rather than cleared by an `except`. `except Exception: status =
    # "failed"` looked equivalent and was not: KeyboardInterrupt, SystemExit
    # and GeneratorExit are BaseException, so Ctrl-C during a slow lens --
    # hygiene shells out to pip-audit, so that is the ordinary case, not an
    # exotic one -- wrote status='complete' on a run holding only the findings
    # recorded before the interrupt. An incomplete run that reads as clean is
    # the exact failure this module exists to prevent.
    #
    # Held on `result` rather than in a local: the same value has to reach both
    # the stored row and every surface that renders the run, and a local meant
    # only the row got it -- `report` then had no way to say the run failed.
    try:
        for name, lens_cfg, pack in plan:
            # `ctx.skips` stays private to this lens (the RunContext default,
            # not result.skips itself): a lens that clears its own list --
            # ctx.skips.clear(), or ctx.skips[:] = [] -- must only be able to
            # erase its own trail. Lens packs are third-party code once the
            # plugin API is public, so a shared list let one misbehaving lens
            # wipe every other lens's skips too; tried that, reverted it.
            #
            # The merge back into result.skips runs in `finally` rather than
            # after the loop, so a lens that skips, yields a candidate, and
            # then raises still gets its skip recorded -- the loss the shared
            # list was originally reaching for, without the blast radius.
            ctx = RunContext(
                project_root=project_root,
                state_root=state_root,
                files=files,
                tier=tier,
                lens_options=lens_cfg.model_dump(exclude_none=True),
                run_id=run_id,
            )
            # `severity_floor` was validated by config and read by nothing, so
            # a configured floor silently did nothing at all. Applied here
            # rather than inside each lens: it is the spine's decision what
            # gets recorded, and a lens is not asked to police itself. What it
            # suppressed is reported, because a filter nobody is told about is
            # the same silence in a nicer costume.
            floor = lens_cfg.severity_floor
            suppressed = 0
            _emit(on_event, kind="lens_started", run_id=run_id, lens=name)
            before_new, before_seen = result.new, result.seen
            try:
                for candidate in pack.run(ctx):
                    if floor is not None and not severity_at_least(
                        candidate.severity, floor
                    ):
                        suppressed += 1
                        continue
                    if upsert(conn, candidate, run_id, _now()):
                        result.new += 1
                    else:
                        result.seen += 1
            finally:
                result.skips.extend(ctx.skips)
                if suppressed:
                    result.skips.append(
                        f"{name}: severity_floor '{floor}' suppressed "
                        f"{suppressed} candidate(s) below that floor; they were "
                        "found but NOT recorded."
                    )
                # In the `finally`, alongside the skip merge and for the same
                # reason: a lens that yields two candidates and then raises has
                # done work worth announcing, and a surface waiting on a
                # `lens_finished` that never arrives shows it as still running
                # forever.
                _emit(
                    on_event,
                    kind="lens_finished",
                    run_id=run_id,
                    lens=name,
                    new=result.new - before_new,
                    seen=result.seen - before_seen,
                    skips=list(ctx.skips),
                )

        _report_unsupported_sinks(cfg, result)
        result.status = "complete"
    finally:
        conn.execute(
            "UPDATE runs SET finished_at = ?, status = ?, skipped_json = ? "
            "WHERE id = ?",
            (_now(), result.status, json.dumps(result.skips), run_id),
        )
        # LAST, and inside the same `finally` that writes the terminal status,
        # so the event and the stored row cannot disagree about how the run
        # ended. A consumer treats this as the end of the stream, so emitting
        # it only on the success path would hang every watcher of a failed run.
        _emit(
            on_event,
            kind="run_finished",
            run_id=run_id,
            status=result.status,
            new=result.new,
            seen=result.seen,
            skips=list(result.skips),
        )

    return result
