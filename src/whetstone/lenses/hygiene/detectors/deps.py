"""Flag dependencies with known advisories, via pip-audit.

Node support lands with M1. This detector deliberately reports *nothing* rather
than guessing when pip-audit is unavailable -- and says so.

pip-audit audits THE AMBIENT PYTHON ENVIRONMENT unless it is handed a target.
Running it with `cwd=project_root` and no positional path does not point it at
the project: nothing in the argv is relative, so `cwd` changes nothing. Measured
against a project pinning requests==2.19.0, that reported 29 packages from
pip-audit's own virtualenv and called the vulnerable pin clean. Installed as a
tool, Whetstone's own environment became what every project on the machine got
audited against. Every path below therefore names its target explicitly, and a
manifest shape that cannot be targeted skips loudly instead of falling back.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from ...base import Candidate, Evidence, EvidenceKind, RunContext, Severity

_PYPROJECT = "pyproject.toml"
_REQUIREMENTS = "requirements.txt"
_SETUP_CFG = "setup.cfg"
_MANIFESTS = (_PYPROJECT, _REQUIREMENTS, _SETUP_CFG)

# The argv prefix that invokes pip-audit, kept as a module constant so tests can
# point it at a script whose output shape they control. A shim on PATH cannot do
# that job portably: Windows CreateProcess resolves only `.exe` from PATH, so
# `pip-audit.bat` is never found by `subprocess` without a shell.
_PIP_AUDIT_ARGV: tuple[str, ...] = ("pip-audit",)

_TIMEOUT_SECONDS = 120
# How long to wait for a killed process tree to release the pipes. Bounded, so
# a kill that does not take cannot reintroduce the unbounded wait it fixes.
_REAP_SECONDS = 15


@dataclass(frozen=True)
class _AuditPlan:
    """What to audit, and the manifest that decided it."""

    args: list[str]
    source: str


def _declares_pep621_project(path: Path) -> bool:
    """True when *path* has a `[project]` table pip-audit can resolve.

    A pyproject.toml holding nothing but `[tool.ruff]` is extremely common and
    declares no dependencies at all; pip-audit refuses it with "pyproject file
    does not contain `project` section". Checking here lets a project like that
    fall through to requirements.txt instead of failing.
    """
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return isinstance(data.get("project"), dict)


def _plan_audit(project_root: Path) -> _AuditPlan | str:
    """Decide what to audit, or return the reason nothing can be.

    Preference order is deliberate. A PEP 621 `[project]` table is the
    project's own declaration of what it depends on, so it wins; pip-audit
    resolves it, which is slower and needs the network, and that is the price
    of auditing what the project actually declares. requirements.txt is the
    fallback for the layout that has no such table.
    """
    pyproject = project_root / _PYPROJECT
    requirements = project_root / _REQUIREMENTS

    if pyproject.is_file() and _declares_pep621_project(pyproject):
        return _AuditPlan([os.fspath(project_root)], _PYPROJECT)
    if requirements.is_file():
        return _AuditPlan(["-r", os.fspath(requirements)], _REQUIREMENTS)
    if pyproject.is_file():
        return (
            f"hygiene/deps: {_PYPROJECT} has no [project] table, so it declares "
            "no dependencies pip-audit can resolve, and no requirements.txt is "
            "present. Advisories were NOT checked. Auditing the ambient Python "
            "environment instead would report on packages this project never "
            "declared."
        )
    if (project_root / _SETUP_CFG).is_file():
        return (
            f"hygiene/deps: only {_SETUP_CFG} was found. pip-audit cannot audit "
            "a setup.cfg project directly, so advisories were NOT checked. Add "
            "a requirements.txt or a PEP 621 [project] table to enable this "
            "check."
        )
    return (
        "hygiene/deps: no Python manifest found "
        f"({', '.join(_MANIFESTS)}); nothing to audit."
    )


def _new_group() -> dict[str, object]:
    """Popen keywords that make the child's whole tree killable as one unit."""
    if os.name == "nt":
        # `taskkill /T` walks the parent/child chain directly, so no group flag
        # is needed -- and CREATE_NEW_PROCESS_GROUP would also detach the child
        # from console signals for no gain here.
        return {}
    return {"start_new_session": True}


def _kill_tree(proc: subprocess.Popen[str]) -> None:
    """Kill the child AND anything it started.

    Killing only the direct child leaves a grandchild holding the inherited
    stdout pipe, so the read that follows never sees EOF. pip-audit shells out
    to pip, so this is the ordinary case, not an exotic one.
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
        proc.kill()


def _run_pip_audit(project_root: Path, args: list[str]) -> str:
    """Run pip-audit against *args* and return raw JSON.

    Raises FileNotFoundError if the tool is absent, TimeoutExpired if it
    outlives its bound, RuntimeError on any other non-success exit.
    """
    argv = [*_PIP_AUDIT_ARGV, "--format", "json", "--progress-spinner", "off", *args]
    proc = subprocess.Popen(
        argv,
        cwd=project_root,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        encoding="utf-8",
        # A byte the child emits that is not valid UTF-8 -- a package name from
        # a private index, a traceback carrying one -- kills the reader thread
        # outright without this. That left stdout None with returncode 0, which
        # the gate below reads as success, and json.loads(None) raises TypeError
        # rather than the JSONDecodeError the caller catches. scope/resolver.py
        # documents the same defect; this call site did not inherit the fix.
        errors="surrogateescape",
        **_new_group(),
    )
    try:
        raw, err = proc.communicate(timeout=_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        _kill_tree(proc)
        # The tree is dead; if the reader threads still have not closed, they
        # are not worth waiting on further. Surfacing the original timeout is
        # what matters, and this wait is bounded so it cannot reintroduce the
        # unbounded one it exists to fix.
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.communicate(timeout=_REAP_SECONDS)
        raise
    # pip-audit exits 1 when it finds vulnerabilities -- that is success for us.
    if proc.returncode not in (0, 1):
        raise RuntimeError((err or "").strip() or f"exit {proc.returncode}")
    return raw or ""


class DepsDetector:
    id = "deps"

    def detect(self, ctx: RunContext) -> Iterator[Candidate]:
        plan = _plan_audit(ctx.project_root)
        if isinstance(plan, str):
            ctx.skip(plan)
            return

        try:
            raw = _run_pip_audit(ctx.project_root, plan.args)
        except FileNotFoundError:
            ctx.skip(
                "hygiene/deps: pip-audit is not installed, so dependency "
                "advisories were NOT checked. Install it with "
                "`uv tool install pip-audit`."
            )
            return
        except (subprocess.TimeoutExpired, RuntimeError) as exc:
            ctx.skip(f"hygiene/deps: pip-audit failed ({exc}); advisories not checked.")
            return

        # Empty covers both the ordinary empty string and the None the reader
        # thread leaves behind when it dies. json.loads(None) is a TypeError,
        # which the JSONDecodeError handler below does not catch.
        if not raw or not raw.strip():
            ctx.skip(
                f"hygiene/deps: pip-audit produced no output while auditing "
                f"{plan.source}; advisories were NOT checked."
            )
            return

        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as exc:
            ctx.skip(f"hygiene/deps: pip-audit returned unparseable JSON ({exc}).")
            return

        # `json.loads` succeeding only proves the output was *some* valid
        # JSON, not that it has pip-audit's expected shape. Validate before
        # walking it, so a malformed entry skips rather than raising out of
        # `detect()` and taking the whole run down.
        if not isinstance(payload, dict):
            ctx.skip(
                "hygiene/deps: pip-audit returned unexpected JSON "
                f"({type(payload).__name__}, expected an object); "
                "advisories not checked."
            )
            return

        dependencies = payload.get("dependencies", [])
        if not isinstance(dependencies, list):
            ctx.skip(
                "hygiene/deps: pip-audit's 'dependencies' field was "
                f"{type(dependencies).__name__}, not a list; advisories not checked."
            )
            return

        for dependency in dependencies:
            if not isinstance(dependency, dict):
                ctx.skip(
                    "hygiene/deps: pip-audit returned a dependency entry that "
                    f"was not an object ({type(dependency).__name__}); skipped "
                    "that entry."
                )
                continue

            name = dependency.get("name", "unknown")
            version = dependency.get("version", "unknown")

            # pip-audit reports a dependency it declined to audit as a
            # `skip_reason` and NO `vulns` key at all, so `.get("vulns", [])`
            # yielded an empty list and the entry vanished. Every editable
            # install (`pip install -e .`, the standard dev layout), every
            # private-index package, and every local wheel produces one. The
            # tool saying it did not audit something is exactly the message
            # this lens exists to pass on.
            skip_reason = dependency.get("skip_reason")
            if skip_reason:
                ctx.skip(
                    f"hygiene/deps: pip-audit declined to audit {name}: "
                    f"{skip_reason}"
                )
                continue

            vulns = dependency.get("vulns", [])
            if not isinstance(vulns, list):
                ctx.skip(
                    f"hygiene/deps: {name}'s 'vulns' field was "
                    f"{type(vulns).__name__}, not a list; that dependency was "
                    "skipped."
                )
                continue

            for vuln in vulns:
                if not isinstance(vuln, dict):
                    ctx.skip(
                        f"hygiene/deps: {name} has a vulnerability entry that "
                        f"was not an object ({type(vuln).__name__}); skipped."
                    )
                    continue

                fixes = vuln.get("fix_versions") or []
                fix_text = (
                    f"Fixed in {', '.join(fixes)}."
                    if fixes
                    else "No fixed version is published yet."
                )
                yield Candidate(
                    lens="hygiene",
                    rule_id=vuln.get("id", "unknown-advisory"),
                    subject=name,
                    title=f"{name} {version} has advisory {vuln.get('id', '?')}",
                    detail=(
                        f"{vuln.get('description', 'No description provided.')}\n"
                        f"{fix_text}"
                    ),
                    severity=Severity.high,
                    evidence=Evidence(
                        kind=EvidenceKind.metric,
                        summary=f"pip-audit advisory {vuln.get('id', '?')}",
                        data={
                            "package": name,
                            "installed": version,
                            "advisory": vuln.get("id"),
                            "fix_versions": fixes,
                            # Which manifest the audit actually resolved. The
                            # defect this replaces was invisible precisely
                            # because nothing recorded what got audited.
                            "audited": plan.source,
                        },
                    ),
                )
