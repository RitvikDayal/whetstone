"""Preflight. Every declared command is executed, not merely read.

A config that says `test: pytest` proves nothing. `doctor` is the difference
between 'configured correctly' and 'ran correctly', and the predecessor lost
days to that distinction.

On shell=True: these commands come from the user's own config, authored by a
human, and need `&&`, pipes, and PATH resolution to be usable. Model-authored
commands are a different category entirely and get exact-match allowlisting in
M1's policy gate. Do not conflate the two.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import signal
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .config.model import WhetstoneConfig
from .errors import UnsafeStatePathError
from .paths import assert_not_cloud_synced

# `dev` is deliberately absent: it starts a long-running server and doctor must
# not launch one. M2's browser lens verifies it by booting it with a readiness
# probe and a timeout.
_VERIFIABLE_COMMANDS = ("install", "test", "lint", "build")

_TIMEOUTS = {"install": 600, "test": 900, "lint": 300, "build": 900}

# How long to wait for a killed process tree to release its pipes. Bounded, so
# the kill that exists to bound a hung command cannot itself reintroduce an
# unbounded wait -- same rationale as lenses/hygiene/detectors/deps.py:_REAP_SECONDS.
_REAP_SECONDS = 15


@dataclass
class CheckResult:
    name: str
    ok: bool
    detail: str
    skipped: bool = False


def _new_group() -> dict[str, object]:
    """Popen keywords that make the child's whole process tree killable as one
    unit. Mirrors lenses/hygiene/detectors/deps.py: a declared `test`, `build`,
    or `lint` command routinely shells out to further children (npm, pnpm,
    pytest-xdist workers), and killing only the direct shell process leaves
    them holding the stdout/stderr pipes open forever.
    """
    if os.name == "nt":
        # `taskkill /T` walks the parent/child chain directly from the PID, so
        # no group flag is needed here.
        return {}
    return {"start_new_session": True}


def _kill_tree(proc: subprocess.Popen[str]) -> None:
    """Kill the child AND anything it started.

    Killing only the direct child leaves a grandchild holding the inherited
    stdout/stderr pipes, so any further read on them never sees EOF. Cannot
    raise: this runs on an exception path and the caller re-raises or returns
    right after it.
    """
    try:
        if os.name == "nt":
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                capture_output=True,
                timeout=_REAP_SECONDS,
            )
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, subprocess.SubprocessError):
        # Already gone, or the platform refused. Fall through to the direct
        # child so the caller is never left waiting on a live process.
        pass
    finally:
        with contextlib.suppress(OSError):
            proc.kill()


def run_command(label: str, command: str, cwd: Path, timeout: int) -> CheckResult:
    """Run *command* through the shell and prove it actually ran.

    Uses Popen with a bounded reap rather than `subprocess.run(timeout=...)`.
    That helper's own timeout handling kills only the direct child and then,
    on Windows, calls `communicate()` a SECOND time with no timeout at all to
    collect the rest of the output -- and that call hangs forever if a
    grandchild the command spawned still holds the stdout/stderr pipes open.
    `pytest` and `npm test` both routinely spawn children, so this is the
    ordinary case, not an exotic one. See lenses/hygiene/detectors/deps.py,
    which hit and fixed the identical defect running pip-audit under a
    timeout; `_new_group`/`_kill_tree` above are the same fix applied here.
    """
    with subprocess.Popen(
        command,
        cwd=cwd,
        shell=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        # A byte a declared command emits that is not valid UTF-8 -- a test
        # runner echoing a non-ASCII fixture path, for instance -- must not
        # crash doctor. `replace` is enough here: unlike deps.py's JSON, this
        # text is only ever displayed, never parsed or stored, so there is no
        # downstream identity or dedupe key that a lost byte could corrupt.
        errors="replace",
        **_new_group(),
    ) as proc:
        try:
            stdout, stderr = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            _kill_tree(proc)
            # Bounded, so a reap that does not take cannot reintroduce the
            # unbounded wait this whole function exists to avoid.
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.communicate(timeout=_REAP_SECONDS)
            return CheckResult(
                name=f"command: {label}",
                ok=False,
                detail=f"`{command}` timed out after {timeout}s.",
            )
        except BaseException:
            # KeyboardInterrupt included: a Ctrl-C mid-command must not leave
            # it, or anything it spawned, running with the pipes still open.
            _kill_tree(proc)
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.communicate(timeout=_REAP_SECONDS)
            raise

    if proc.returncode == 0:
        return CheckResult(f"command: {label}", True, f"`{command}` exited 0.")

    tail = (stderr or stdout or "").strip().splitlines()[-5:]
    return CheckResult(
        name=f"command: {label}",
        ok=False,
        detail=f"`{command}` exited {proc.returncode}. " + " ".join(tail),
    )


def run_doctor(
    cfg: WhetstoneConfig, project_root: Path, state_root: Path
) -> list[CheckResult]:
    results: list[CheckResult] = [
        _check_git(project_root),
        _check_state_path(state_root),
    ]

    for label in _VERIFIABLE_COMMANDS:
        command = getattr(cfg.environment.commands, label)
        if not command:
            results.append(
                CheckResult(
                    f"command: {label}", True, "not declared; nothing to verify.", True
                )
            )
            continue
        results.append(run_command(label, command, project_root, _TIMEOUTS[label]))

    dev = cfg.environment.commands.dev
    results.append(
        CheckResult(
            "command: dev",
            True,
            (
                f"declared as `{dev}`; not launched by doctor (long-running)."
                if dev
                else "not declared; nothing to verify."
            ),
            True,
        )
    )
    return results


def _check_git(project_root: Path) -> CheckResult:
    if shutil.which("git") is None:
        return CheckResult("git", False, "git is not on PATH.")
    if not (project_root / ".git").exists():
        return CheckResult("git", False, f"{project_root} is not a git repository.")
    return CheckResult("git", True, "repository detected.")


def _check_state_path(state_root: Path) -> CheckResult:
    try:
        assert_not_cloud_synced(state_root)
    except UnsafeStatePathError as exc:
        return CheckResult("state path", False, str(exc))
    return CheckResult("state path", True, f"{state_root} is local.")
