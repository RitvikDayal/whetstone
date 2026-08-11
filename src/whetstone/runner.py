"""Orchestrates one run. Owns skip reporting; a skip that is not reported is a bug."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .config.model import BoundariesConfig, LensConfig, WhetstoneConfig
from .lenses.base import LensPack, LensScope, RunContext, lens_scope
from .lenses.registry import get_lens
from .scope.resolver import resolve_files
from .severity import severity_at_least
from .store.findings import upsert


@dataclass
class RunResult:
    run_id: str
    tier: str
    file_count: int
    new: int = 0
    seen: int = 0
    skips: list[str] = field(default_factory=list)


def _now() -> str:
    return datetime.now(UTC).isoformat()


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


def _report_unsupported_sinks(cfg: WhetstoneConfig, result: RunResult) -> None:
    for sink in cfg.sinks:
        if sink.kind not in _IMPLEMENTED_SINKS:
            result.skips.append(
                f"sink '{sink.kind}': not implemented in this version; findings "
                "were NOT published there. Only the built-in local sink is "
                "available."
            )


def execute_run(
    conn: sqlite3.Connection,
    cfg: WhetstoneConfig,
    project_root: Path,
    state_root: Path,
    *,
    tier: str,
    changed_only: bool,
) -> RunResult:
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
    skips: list[str] = []
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

        plan.append((name, lens_cfg, pack))

    file_scoped = [name for name, _, pack in plan if lens_scope(pack) is LensScope.file]
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

    if _boundaries_are_narrowed(cfg.boundaries):
        for name, _, pack in plan:
            if lens_scope(pack) is LensScope.project:
                skips.append(
                    f"{name}: project-scoped. It reads fixed project artifacts "
                    "and never consults boundaries.include/exclude, so those "
                    "patterns did NOT narrow what it examined."
                )

    result = RunResult(run_id=run_id, tier=tier, file_count=len(files), skips=skips)

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

    # Once the `runs` row exists, every exit path -- including a lens raising
    # mid-iteration -- must close it out with finished_at and a terminal
    # status. A row stuck at status='running' forever is its own kind of
    # silent skip: nothing ever tells the user the run didn't finish.
    status = "complete"
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

        _report_unsupported_sinks(cfg, result)
    except Exception:
        status = "failed"
        raise
    finally:
        conn.execute(
            "UPDATE runs SET finished_at = ?, status = ?, skipped_json = ? "
            "WHERE id = ?",
            (_now(), status, json.dumps(result.skips), run_id),
        )

    return result
