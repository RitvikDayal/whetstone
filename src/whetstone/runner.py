"""Orchestrates one run. Owns skip reporting; a skip that is not reported is a bug."""

from __future__ import annotations

import json
import sqlite3
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .config.model import WhetstoneConfig
from .lenses.base import RunContext
from .lenses.registry import get_lens
from .scope.resolver import resolve_files
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


def _report_unsupported_sinks(cfg: WhetstoneConfig, result: RunResult) -> None:
    for sink in cfg.sinks:
        if sink.kind not in _IMPLEMENTED_SINKS:
            result.skips.append(
                f"sink '{sink.kind}': not implemented in this version; findings "
                "were NOT published there."
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

    # Raises GitError on no merge base or undecodable tracked paths inside the
    # boundaries. Deliberately left to propagate rather than swallowed into a
    # skip: no `runs` row exists yet at this point (the INSERT below hasn't
    # happened), there is no file count and no scope to report a partial run
    # against, and resolve_files itself treats both cases as hard stops for the
    # same reason this whole module exists -- a scan that quietly examined less
    # than it claims is worse than one that refuses to start. The caller (CLI)
    # is responsible for turning this into a clean error message.
    files = resolve_files(
        project_root,
        cfg.boundaries,
        changed_only=changed_only,
        base_branch=cfg.project.forge.base_branch,
    )
    result = RunResult(run_id=run_id, tier=tier, file_count=len(files))

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
        for name, lens_cfg in cfg.lenses.items():
            if not lens_cfg.enabled:
                result.skips.append(f"{name}: disabled in config; not run.")
                continue

            pack = get_lens(name)
            if pack is None:
                result.skips.append(
                    f"{name}: not installed; no lens pack with that name is "
                    "registered. Not run."
                )
                continue

            if not pack.supports_tier(tier):
                result.skips.append(
                    f"{name}: not available at tier '{tier}'; not run. "
                    "Use a higher tier to include it."
                )
                continue

            ctx = RunContext(
                project_root=project_root,
                state_root=state_root,
                files=files,
                tier=tier,
                lens_options=lens_cfg.model_dump(exclude_none=True),
                run_id=run_id,
            )
            for candidate in pack.run(ctx):
                if upsert(conn, candidate, run_id, _now()):
                    result.new += 1
                else:
                    result.seen += 1
            result.skips.extend(ctx.skips)

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
