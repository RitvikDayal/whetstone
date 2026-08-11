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
import re
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


def _pyproject_state(path: Path) -> str:
    """Classify a pyproject.toml: "declares", "no-project", or "unreadable".

    A pyproject holding nothing but `[tool.ruff]` is extremely common and
    declares no dependencies at all; pip-audit refuses it with "pyproject file
    does not contain `project` section". Recognising that lets such a project
    fall through to requirements.txt instead of failing.

    "unreadable" is kept separate from "no-project" because they are different
    problems with different fixes, and reporting a broken file as a missing
    table sends the user looking in the wrong place.
    """
    try:
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return "unreadable"
    return "declares" if isinstance(data.get("project"), dict) else "no-project"


# Text that cannot be encoded as UTF-8, so it cannot be stored, printed, or
# acted on. `errors="surrogateescape"` above is what keeps a non-UTF-8 byte
# from killing the reader thread, but decoding is only half the job: the
# resolver pairs it with a containment check (scope/resolver.py:29) precisely
# so surrogates never travel downstream. This detector had the decode and not
# the guard, so a bad byte inside a package name or an advisory description
# reached sqlite as a lone surrogate and killed the whole run with
# UnicodeEncodeError -- from `runner.upsert`, which is outside the
# per-detector guard in pack.py and so was not caught by it.
#
# Wider than the resolver's `[\udc80-\udcff]` on purpose. That range is exactly
# what surrogateescape produces from bytes. This input is JSON, which can also
# carry an explicit `\ud800` escape that json.loads decodes into a lone
# surrogate without any byte ever being malformed.
#
# The right long-term home for this is `Evidence`/`Candidate` refusing
# unstorable text at construction, so every lens inherits it instead of each
# one remembering -- see issue #14. Until then this is the second copy, and
# the comment above names the first.
_SURROGATE = re.compile("[\ud800-\udfff]")


def _unstorable(value: object) -> bool:
    """True when *value* holds text UTF-8 cannot represent."""
    if isinstance(value, str):
        return _SURROGATE.search(value) is not None
    if isinstance(value, (list, tuple)):
        return any(_unstorable(item) for item in value)
    return False


def _as_text(value: object) -> str | None:
    """The value when it is text, otherwise None.

    Deliberately not `str(value)`. Container types are validated below, and the
    scalars pulled out of them were not: `{"name": 42}` or `{"id": {"a": 1}}`
    passes every shape check, and `_unstorable` returns False for anything that
    is neither str nor sequence. The value then reaches `Candidate.subject`,
    whose `dedupe_key` calls `.replace()` on it, and `upsert` binds it to
    sqlite -- both in runner.py, OUTSIDE the per-detector guard in pack.py, so
    the run ends `failed` with no skip line. Exactly the path the surrogate
    comment above describes. Coercing instead of refusing would invent an
    identity that then feeds the dedupe key.

    Same long-term home as `_SURROGATE`: `Candidate` refusing unusable values
    at construction, so every lens inherits it -- see issue #14.
    """
    return value if isinstance(value, str) else None


def _as_versions(value: object) -> list[str] | None:
    """A list of version strings, otherwise None.

    A bare string passes `_unstorable`, and then `', '.join("2.0")` renders
    `2, ., 0` while `evidence.data["fix_versions"]` stores a string where every
    consumer expects a list.
    """
    if isinstance(value, (list, tuple)) and all(isinstance(v, str) for v in value):
        return list(value)
    return None


def _storable(text: str) -> str:
    """Replace unencodable characters with a visible escape.

    `backslashreplace` rather than `replace`: `caf\\udce9` says which byte was
    lost, where `caf?` throws that away too.
    """
    return text.encode("utf-8", "backslashreplace").decode("utf-8")


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
    state = _pyproject_state(pyproject) if pyproject.is_file() else "absent"

    if state == "declares":
        return _AuditPlan([os.fspath(project_root)], _PYPROJECT)
    if requirements.is_file():
        return _AuditPlan(["-r", os.fspath(requirements)], _REQUIREMENTS)
    if state == "unreadable":
        return (
            f"hygiene/deps: {_PYPROJECT} could not be parsed as TOML, so the "
            "dependencies it declares could not be read, and no requirements.txt "
            "is present. Advisories were NOT checked. Fix the file or add a "
            "requirements.txt."
        )
    if state == "no-project":
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
    # `with`, so the pipes are closed and the child reaped on every exit, not
    # just the two this function names. Note that Popen.__exit__ ends in an
    # UNBOUNDED wait(), which is why the handler below kills the tree first:
    # the context manager is what guarantees cleanup, the kill is what keeps
    # that cleanup bounded.
    with subprocess.Popen(
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
        #
        # Decoding is only half of it. Surrogates that survive the decode are
        # contained before they leave `detect()` -- see `_SURROGATE`. Porting
        # the decode without the containment just moves the crash from
        # json.loads to sqlite.
        errors="surrogateescape",
        **_new_group(),
    ) as proc:
        try:
            raw, err = proc.communicate(timeout=_TIMEOUT_SECONDS)
        except BaseException:
            # BaseException, not TimeoutExpired: Ctrl-C in the middle of a slow
            # audit used to propagate straight past this and leave pip-audit --
            # and the pip it shelled out to -- running with the pipes still
            # open. The timeout is not the only way out of a communicate().
            _kill_tree(proc)
            # The tree is dead; if the reader threads still have not closed,
            # they are not worth waiting on further. Surfacing the original
            # exception is what matters, and this wait is bounded so it cannot
            # reintroduce the unbounded one it exists to fix.
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.communicate(timeout=_REAP_SECONDS)
            raise
        # pip-audit exits 1 when it finds vulnerabilities -- success for us.
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

            name = _as_text(dependency.get("name", "unknown"))
            version = _as_text(dependency.get("version", "unknown"))
            if name is None or version is None:
                ctx.skip(
                    "hygiene/deps: pip-audit reported a dependency whose name or "
                    f"version was not text (name={dependency.get('name')!r}, "
                    f"version={dependency.get('version')!r}); it cannot be stored "
                    "or acted on, so its advisories were NOT recorded."
                )
                continue

            # Identity and remedy have to be representable or the finding is
            # not one anybody can act on: you cannot `pip install --upgrade` a
            # name you cannot type, and `subject` feeds the dedupe key. Drop
            # the whole entry and say so, rather than storing text that kills
            # the run three frames later inside `upsert`.
            if _unstorable(name) or _unstorable(version):
                ctx.skip(
                    "hygiene/deps: pip-audit reported a dependency whose name "
                    f"or version is not valid UTF-8 (name={ascii(name)}, "
                    f"version={ascii(version)}). It cannot be stored or acted "
                    "on, so its advisories were NOT recorded."
                )
                continue

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
                    f"{_storable(str(skip_reason))}"
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

                advisory = _as_text(vuln.get("id", "unknown-advisory"))
                fixes = _as_versions(vuln.get("fix_versions") or [])
                if advisory is None or fixes is None:
                    ctx.skip(
                        f"hygiene/deps: {name} has an advisory whose id or fix "
                        f"versions were not text (id={vuln.get('id')!r}, "
                        f"fix_versions={vuln.get('fix_versions')!r}); it was NOT "
                        "recorded."
                    )
                    continue

                # Same rule as the package name: the advisory id is the finding's
                # identity and the fix versions are the remedy, so neither can be
                # text nobody can store or type.
                if _unstorable(advisory) or _unstorable(fixes):
                    ctx.skip(
                        f"hygiene/deps: {name} has an advisory whose id or fix "
                        f"versions are not valid UTF-8 (id={ascii(advisory)}); "
                        "it was NOT recorded."
                    )
                    continue

                description = _as_text(
                    vuln.get("description", "No description provided.")
                )
                if description is None:
                    # Prose, unlike identity, is not what the user acts on, so
                    # a wrong-typed description does not cost a real advisory
                    # the way a wrong-typed id does. Say what was dropped.
                    ctx.skip(
                        f"hygiene/deps: {name} advisory {advisory} had a "
                        f"description that was not text "
                        f"({type(vuln.get('description')).__name__}); the finding "
                        "was recorded without it."
                    )
                    description = "No description provided."
                elif _unstorable(description):
                    # Unlike identity, prose is not what the user acts on. A
                    # real advisory is not worth discarding over one bad byte in
                    # its text, so the text is escaped and the substitution is
                    # reported -- the stored detail is then not verbatim what
                    # the tool emitted, and that has to be said out loud.
                    ctx.skip(
                        f"hygiene/deps: {name} advisory {advisory} has a "
                        "description containing bytes that are not valid UTF-8. "
                        "The finding was recorded with those bytes escaped, so "
                        "its description is not verbatim."
                    )
                    description = _storable(description)

                fix_text = (
                    f"Fixed in {', '.join(fixes)}."
                    if fixes
                    else "No fixed version is published yet."
                )
                yield Candidate(
                    lens="hygiene",
                    rule_id=advisory,
                    subject=name,
                    title=f"{name} {version} has advisory {advisory}",
                    detail=f"{description}\n{fix_text}",
                    severity=Severity.high,
                    evidence=Evidence(
                        kind=EvidenceKind.metric,
                        summary=f"pip-audit advisory {advisory}",
                        data={
                            "package": name,
                            "installed": version,
                            "advisory": advisory,
                            "fix_versions": fixes,
                            # Which manifest the audit actually resolved. The
                            # defect this replaces was invisible precisely
                            # because nothing recorded what got audited.
                            "audited": plan.source,
                        },
                    ),
                )
