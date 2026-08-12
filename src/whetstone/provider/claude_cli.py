"""Drive the Claude Code CLI as a subprocess.

The only module that knows what flags the binary takes. Everything above it
speaks `StageRequest` and `StageResult`.

WHAT THIS WAS WRITTEN AGAINST: CLI 2.1.224, invoked for real before a line of
it existed. The first specification of this module was written without running
the binary and got two things wrong that would have shipped: it bounded stages
with `--max-turns`, which does not exist, and it wrote the schema to a file,
which `--json-schema` does not take. Both are recorded in the plan's change log.

THE ENVELOPE. `--output-format json` returns one object, and the payload is
NESTED inside it as `structured_output` rather than being the whole of stdout:

    {"is_error": false, "subtype": "success",
     "result": "{\\"findings\\": []}",           <- the same content as text
     "structured_output": {"findings": []},    <- what the spine wants
     "usage": {...}, "total_cost_usd": 0.43, "duration_ms": 4754}

`is_error` lives in that payload and is NOT the exit code. A refusal can arrive
with status 0, so the exit code alone cannot classify the outcome.
"""

from __future__ import annotations

import json
import subprocess
import time
from typing import Any

import jsonschema

from .._subprocess import close_pipes, kill_and_reap, new_group
from ..policy.gate import PermissionSet
from .base import StageRequest, StageResult, Usage

# The argv prefix that invokes the CLI, kept as a module constant so tests can
# point it at a script whose output shape they control. A shim on PATH cannot do
# that job portably: Windows CreateProcess resolves only `.exe` from PATH, so
# `claude.bat` is never found by `subprocess` without a shell. Same seam as
# `deps.py`'s `_PIP_AUDIT_ARGV`, adopted there after every test replaced the
# function itself and four defects lived inside it at once.
_CLAUDE_ARGV: tuple[str, ...] = ("claude",)

# Pinned rather than inherited. The operator's default model would make two runs
# on two machines incomparable, and a run whose price depends on an ambient
# setting cannot be estimated within 2x of anything.
_MODEL = "sonnet"

_TIMEOUT_SECONDS = 900

# Meta keys the CLI cannot resolve: it does not fetch the 2020-12 meta-schema
# `$ref`. Stripped rather than rejected, because the schemas on disk are
# legitimately written with them for editor tooling.
_SCHEMA_META_KEYS = ("$schema", "title")


def _schema_for_cli(schema: dict[str, Any]) -> str:
    return json.dumps({k: v for k, v in schema.items() if k not in _SCHEMA_META_KEYS})


def _tool_list(tools: frozenset[str]) -> str:
    """The CLI takes a comma-or-space separated list. Sorted, so an argv is
    reproducible and a diff of two runs is readable."""
    return ",".join(sorted(tools))


def _usage_from(envelope: dict[str, Any], wall_seconds: float) -> Usage:
    """Read what the stage cost, defaulting every field to zero rather than None.

    `wall_seconds` is OURS, measured around the subprocess. The envelope carries
    its own `duration_ms`, and that is the tool describing itself; a claim with
    a physical referent is recomputed from the world rather than believed.
    """
    raw = envelope.get("usage") or {}
    cost = envelope.get("total_cost_usd")
    return Usage(
        input_tokens=int(raw.get("input_tokens") or 0),
        output_tokens=int(raw.get("output_tokens") or 0),
        cache_creation_input_tokens=int(raw.get("cache_creation_input_tokens") or 0),
        cache_read_input_tokens=int(raw.get("cache_read_input_tokens") or 0),
        cost_usd=float(cost) if isinstance(cost, (int, float)) else None,
        wall_seconds=wall_seconds,
    )


def _argv(request: StageRequest) -> list[str]:
    """Build the command line. Every flag here was read off `claude --help`.

    THE ISOLATION FLAGS, and why they are not decoration. A measured trivial
    call spent 41,036 cache-creation tokens and $0.43 before doing any work,
    because the subprocess inherited the operator's global configuration:
    plugin MCP servers and their tool definitions, for tools the policy gate
    then denies. Whetstone pays that on every stage of every run.

    `--bare` would cut more and is unavailable: it forces ANTHROPIC_API_KEY or
    apiKeyHelper auth and never reads OAuth, so it is closed to a subscription.
    """
    permissions: PermissionSet | None = request.permissions
    argv = [
        *_CLAUDE_ARGV,
        "-p",
        "--output-format",
        "json",
        "--json-schema",
        _schema_for_cli(request.schema),
        "--model",
        _MODEL,
        "--effort",
        request.effort,
        # Only MCP servers from --mcp-config, of which we pass none.
        "--strict-mcp-config",
        # A stage is not resumable and should not leave session state behind.
        "--no-session-persistence",
        "--add-dir",
        str(request.cwd),
    ]
    if permissions is not None:
        if permissions.allowed_tools:
            argv += ["--allowedTools", _tool_list(permissions.allowed_tools)]
        if permissions.denied_tools:
            argv += ["--disallowedTools", _tool_list(permissions.denied_tools)]
    if request.max_budget_usd is not None:
        argv += ["--max-budget-usd", str(request.max_budget_usd)]
    return argv


class ClaudeCliProvider:
    """Runs one stage. Decides nothing about what comes back."""

    name = "claude-cli"

    def run_stage(self, request: StageRequest) -> StageResult:
        started = time.monotonic()
        try:
            raw, err, code = self._invoke(request)
        except FileNotFoundError:
            return self._failed(
                "the `claude` CLI is not installed, or is not on PATH, so no "
                "model stage could run. Install Claude Code and re-run.",
                started,
            )
        except subprocess.TimeoutExpired:
            return self._failed(
                f"the `claude` CLI did not finish within {_TIMEOUT_SECONDS}s "
                "and was killed. The stage produced nothing.",
                started,
            )
        return self._interpret(raw, err, code, request, started)

    def _invoke(self, request: StageRequest) -> tuple[str, str, int]:
        # Not a `with`. `Popen.__exit__` ends in an unbounded `wait()` that
        # defeats the timeout this call depends on; see `_subprocess.py`.
        proc = subprocess.Popen(
            _argv(request),
            cwd=request.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            # A byte that is not valid UTF-8 kills the reader thread outright
            # without this, leaving stdout None with returncode 0 -- success by
            # the gate below, and a TypeError two frames later. Two call sites
            # in this repo have already shipped that defect.
            errors="surrogateescape",
            **new_group(),
        )
        try:
            raw, err = proc.communicate(request.prompt, timeout=_TIMEOUT_SECONDS)
        except BaseException:
            # BaseException, not TimeoutExpired: Ctrl-C mid-stage otherwise
            # leaves the CLI running with the pipes open.
            kill_and_reap(proc)
            raise
        close_pipes(proc)
        return raw or "", err or "", proc.returncode

    def _interpret(
        self,
        raw: str,
        err: str,
        code: int,
        request: StageRequest,
        started: float,
    ) -> StageResult:
        try:
            envelope = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            # A non-zero exit with unreadable stdout is the tool failing, not
            # the model declining, and the two get different sentences.
            if code != 0:
                detail = err.strip() or "no stderr"
                return self._failed(
                    f"the `claude` CLI exited {code} without usable output: "
                    f"{detail}",
                    started,
                    raw=raw,
                )
            return self._failed(
                "the `claude` CLI returned output that is not JSON, so the "
                "stage cannot be read. Refusing rather than guessing at it.",
                started,
                raw=raw,
            )
        if not isinstance(envelope, dict):
            return self._failed(
                "the `claude` CLI returned JSON that is not an object, so it "
                "carries no envelope to read.",
                started,
                raw=raw,
            )

        usage = _usage_from(envelope, time.monotonic() - started)
        if envelope.get("is_error"):
            reason = (
                str(envelope.get("result") or "").strip()
                or str(envelope.get("api_error_status") or "").strip()
                or f"subtype {envelope.get('subtype')!r}"
            )
            return StageResult(
                ok=False, data=None, raw=raw, usage=usage, error=reason
            )

        data = envelope.get("structured_output")
        if not isinstance(data, dict):
            return StageResult(
                ok=False,
                data=None,
                raw=raw,
                usage=usage,
                error=(
                    "the envelope carried no structured_output object, so the "
                    "stage produced no payload the spine can act on."
                ),
            )
        try:
            # Validated HERE as well as by the CLI. The CLI's own validation is
            # the model's side of the claim, and a model's self-assessment is
            # never trusted -- if its enforcement changes, the spine must not
            # silently start accepting a wider shape.
            jsonschema.validate(data, request.schema)
        except jsonschema.ValidationError as exc:
            return StageResult(
                ok=False,
                data=None,
                raw=raw,
                usage=usage,
                error=(
                    f"the stage payload does not match its schema: {exc.message}"
                ),
            )
        return StageResult(ok=True, data=data, raw=raw, usage=usage, error=None)

    def _failed(self, message: str, started: float, raw: str = "") -> StageResult:
        """A failure still cost wall time, and usually money. Reporting zero
        usage for it would under-report every run that went wrong."""
        return StageResult(
            ok=False,
            data=None,
            raw=raw,
            usage=Usage(wall_seconds=time.monotonic() - started),
            error=message,
        )
