"""Drive the Claude Code CLI as a subprocess.

The only module that knows what flags the binary takes. Everything above it
speaks `StageRequest` and `StageResult`.

WHAT THIS WAS WRITTEN AGAINST: CLI 2.1.224, invoked for real. The first
specification of this module was written without running the binary and got two
things wrong that would have shipped: it bounded stages with `--max-turns`,
which does not exist, and it wrote the schema to a file, which `--json-schema`
does not take. The SECOND version ran the binary and still got the permission
model wrong, which is the more instructive failure -- see `--tools` below.

THE TWO TOOL FLAGS, AND WHY MIXING THEM UP COST A BRANCH:

    --tools         the tools that EXIST for this stage        <- the bound
    --allowedTools  the tools that need no approval            <- convenience

The first version sent only `--allowedTools`, so every stage held the CLI's
full default toolset. A reviewer drove a read-only `reproduce` stage into
creating files, appending to README.md and running `git init`. Every unit test
was green: they asserted the flag reached the argv, and it did. Both flags are
VARIADIC and are passed as separate argv entries -- the old comma join also
broke the scoped form `Bash(uv run pytest -q)`, which contains spaces, so the
one mechanism that could scope Bash was silently unusable.

THE ENVELOPE. `--output-format json` returns one object, and the payload is
NESTED inside it as `structured_output` rather than being the whole of stdout:

    {"is_error": false, "subtype": "success",
     "result": "{\\"findings\\": []}",           <- the same content as text
     "structured_output": {"findings": []},    <- what the spine wants
     "usage": {...}, "total_cost_usd": 0.43, "duration_ms": 4754}

`is_error` lives in that payload and is NOT the exit code. A refusal can arrive
with status 0, so the exit code alone cannot classify the outcome -- and a
non-zero exit alongside a well-formed envelope is still a failure, which is the
direction the first version got wrong.
"""

from __future__ import annotations

import json
import math
import os
import subprocess
import time
from typing import Any

import jsonschema

from .._subprocess import close_pipes, kill_and_reap, new_group
from ..lenses.base import unstorable
from ..policy.gate import PermissionSet, PolicyError
from . import sentinel
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

# Windows caps a command line at 32767 characters including the terminator. The
# schema goes inline, so a large one hits this rather than any Python limit --
# and CreateProcess reports it as errno 206, which the first version's
# FileNotFoundError arm rendered as "Claude Code is not installed" and sent the
# reader off to reinstall a binary that was there all along.
_WINDOWS_ARGV_LIMIT = 32000

# Whether to enforce the cap. A module constant rather than an inline
# `os.name == "nt"` so the check is reachable from a test on EVERY leg: with the
# platform test inlined, the whole function was dead code on Ubuntu, and a
# mutation to its comparison went red on Windows only. Two legs that do not
# prove the same things are two legs that can disagree about whether a fix
# landed.
_ENFORCE_ARGV_LIMIT = os.name == "nt"

# The environment the CLI is allowed to inherit, and nothing else.
#
# `Popen` was called with no `env=`, and the isolation docstring below claimed
# freedom from "the operator's global configuration" -- which was true of
# SETTINGS FILES only. `ANTHROPIC_BASE_URL=http://127.0.0.1:1` reached the CLI
# and redirected the API endpoint, proven by a 45s hang against a 2s control.
# `ANTHROPIC_API_KEY`, `ANTHROPIC_MODEL`, `CLAUDE_CODE_*` and the proxy
# variables ride the same channel, and the stage prompt contains repo source, so
# that is a live route for it to leave for a third party.
#
# An ALLOW-list, not a deny-list: a deny-list of a vendor's environment
# variables is out of date the day they add one, and this module has already
# been wrong once about what somebody else's binary reads.
_ENV_ALLOWED = (
    # Process basics. Without these the CLI cannot find its own runtime.
    "PATH", "HOME", "SHELL", "LANG", "LC_ALL", "TMPDIR", "TZ",
    # Windows equivalents.
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT",
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
    "PROGRAMFILES", "PROGRAMFILES(X86)", "PROGRAMDATA", "TEMP", "TMP",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "OS",
)


def _child_env() -> dict[str, str]:
    """The environment a stage runs under. See `_ENV_ALLOWED`."""
    return {
        name: value
        for name, value in os.environ.items()
        if name.upper() in _ENV_ALLOWED
    }


def _schema_for_cli(schema: dict[str, Any]) -> str:
    return json.dumps({k: v for k, v in schema.items() if k not in _SCHEMA_META_KEYS})


def _int(value: object) -> int:
    """A token count from a source that is not obliged to be sane.

    `int(raw.get(...))` was the first spelling, and it raises `ValueError` on
    `"12.5"`, `TypeError` on a dict and `AttributeError` on a non-mapping
    `usage` -- three uncaught exception types, all reachable from a field the
    model's runtime fills in. `deps.py` validates field by field for the same
    reason; this is that, smaller.

    THE FOURTH TYPE IS `OverflowError`, and it is the one that got through.
    `json.loads` accepts bare `Infinity`, `-Infinity` and `NaN` with no flag, so
    `{"usage": {"input_tokens": Infinity}}` is an ordinary parseable envelope.
    `int(float("inf"))` raises OverflowError, which is neither of the two the
    `except` named, so it propagated out of `run_stage` -- and a provider that
    raises has broken its only contract. `"1e400"` overflows inside `float()`
    and arrives the same way.
    """
    if isinstance(value, bool):  # bool is an int subclass and never a count
        return 0
    if isinstance(value, int):
        return value if value >= 0 else 0
    if isinstance(value, float):
        # `math.isfinite` rather than a wider `except`: infinity is not a large
        # count, it is an absent one, and rounding it to sys.maxsize would put
        # a number in a budget report that no stage ever spent.
        return int(value) if math.isfinite(value) and value >= 0 else 0
    if isinstance(value, str):
        try:
            parsed = float(value)
        except ValueError:
            return 0
        return int(parsed) if math.isfinite(parsed) and parsed >= 0 else 0
    return 0


def _float_or_none(value: object) -> float | None:
    """A cost, or None when there is not one.

    Non-finite is None rather than itself: `inf` and `nan` poison every sum and
    comparison downstream, and `nan` in particular compares False against
    everything, so a budget ceiling silently stops working. Unknown is the
    honest reading of both.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value) if math.isfinite(value) else None
    if isinstance(value, str):
        try:
            parsed = float(value)
        except ValueError:
            return None
        return parsed if math.isfinite(parsed) else None
    return None


# `usage` is snake_case; `modelUsage`'s per-model entries are camelCase. Both
# spellings are accepted for both, because guessing wrong here reports zero.
_TOKEN_KEYS = {
    "input_tokens": ("input_tokens", "inputTokens"),
    "output_tokens": ("output_tokens", "outputTokens"),
    "cache_creation_input_tokens": (
        "cache_creation_input_tokens",
        "cacheCreationInputTokens",
    ),
    "cache_read_input_tokens": (
        "cache_read_input_tokens",
        "cacheReadInputTokens",
    ),
}


def _tokens(block: object) -> dict[str, int]:
    if not isinstance(block, dict):
        return dict.fromkeys(_TOKEN_KEYS, 0)
    return {
        field: max(_int(block.get(key)) for key in keys)
        for field, keys in _TOKEN_KEYS.items()
    }


def _model_usage_tokens(envelope: dict[str, Any]) -> dict[str, int]:
    """`modelUsage` keyed by model name, summed across models."""
    block = envelope.get("modelUsage")
    if not isinstance(block, dict):
        return dict.fromkeys(_TOKEN_KEYS, 0)
    totals = dict.fromkeys(_TOKEN_KEYS, 0)
    for per_model in block.values():
        for field, count in _tokens(per_model).items():
            totals[field] += count
    return totals


def _usage_from(envelope: dict[str, Any], wall_seconds: float) -> Usage:
    """Read what the stage cost, defaulting every field to zero rather than None.

    BOTH `usage` AND `modelUsage`, taking the larger of the two per field. A
    real budget-exhausted envelope carried an all-zero `usage` next to a
    `modelUsage` reporting 47,661 cache-creation tokens, and the first version
    read only the former -- so a stage that burned 47k tokens reported
    `total_tokens == 0`. The hand-written fixture put the numbers where the code
    looked, which is exactly why the suite stayed green over it.

    `wall_seconds` is OURS, measured around the subprocess. The envelope carries
    its own `duration_ms`, and that is the tool describing itself; a claim with
    a physical referent is recomputed from the world rather than believed.
    """
    top = _tokens(envelope.get("usage"))
    per_model = _model_usage_tokens(envelope)
    merged = {field: max(top[field], per_model[field]) for field in _TOKEN_KEYS}

    if any(per_model.values()) and per_model != top:
        source = "modelUsage" if per_model == merged else "usage+modelUsage"
    elif any(top.values()):
        source = "usage"
    else:
        source = "none"

    return Usage(
        **merged,
        cost_usd=_float_or_none(envelope.get("total_cost_usd")),
        wall_seconds=wall_seconds,
        source=source,
    )


def _denials_from(envelope: dict[str, Any]) -> tuple[str, ...]:
    """What the CLI refused.

    NECESSARY AND NOT SUFFICIENT, and the docstring says so because it would be
    easy to mistake this for the check. Both halves are measured against the
    real binary:

    - A tool that is AVAILABLE and not auto-approved is refused, and every
      refusal is recorded. The `reproduce` profile grants Bash without
      approving any command; the model attempted Bash eight times across
      thirteen turns and this came back with eight entries.
    - A tool that is ABSENT from `--tools` produces an EMPTY list, because
      absence is not refusal and there was never a call to refuse.

    So an empty list means "nothing was refused", never "nothing was blocked".
    `sentinel.py` is what sees through the second case.
    """
    raw = envelope.get("permission_denials")
    if raw is None:
        return ()
    if not isinstance(raw, list):
        # FAIL CLOSED. Returning () for an unreadable field means "nothing was
        # refused", which is a claim -- and this module's whole thesis is that
        # the CLI's envelope shape changes underneath it.
        return ("<unreadable permission_denials>",)
    out = []
    for item in raw:
        if isinstance(item, dict):
            name = item.get("tool_name") or item.get("tool") or "unnamed tool"
            out.append(str(name))
        elif item is not None:
            out.append(str(item))
    return tuple(out)


def _argv(request: StageRequest) -> list[str]:
    """Build the command line. Every flag here was read off `claude --help` and
    then confirmed by running the binary, in that order, because reading was
    not enough the last two times.

    THE ISOLATION FLAGS. A measured trivial call spent 41,036 cache-creation
    tokens and $0.43 before doing any work, because the subprocess inherited the
    operator's global configuration: plugin MCP servers and their tool
    definitions, for tools the policy gate then denies. `--setting-sources ""`
    is the lever that actually cuts it -- measured at 12,365 creation tokens and
    $0.08 for the same work. `--strict-mcp-config` and `--no-session-persistence`
    were chosen first, without measuring, and do almost nothing on their own.

    `--bare` would cut more and is unavailable: it forces ANTHROPIC_API_KEY or
    apiKeyHelper auth and never reads OAuth, so it is closed to a subscription.
    """
    permissions = request.permissions
    if not isinstance(permissions, PermissionSet):
        raise PolicyError(
            f"stage {request.stage!r} was built with permissions="
            f"{permissions!r}. A stage runs under a policy or it does not run: "
            f"the first version treated absence as 'send no permission flags', "
            f"so no policy and the strictest policy expressible produced the "
            f"same, least restrictive, command line."
        )
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
        # The operator's user/project/local settings are NOT loaded. Isolation
        # and cost both; see the docstring.
        "--setting-sources",
        "",
        # Pinned, not inherited. Every "an unapproved tool is a refused tool"
        # property rested on the CLI's DEFAULT for this, which is not something
        # the argv asserted anywhere -- the same shape as trusting a flag's
        # meaning without sending it.
        "--permission-mode",
        "default",
        # Only MCP servers from --mcp-config, of which we pass none.
        "--strict-mcp-config",
        # A stage is not resumable and should not leave session state behind.
        "--no-session-persistence",
        "--add-dir",
        str(request.cwd),
    ]
    # ALWAYS sent, including when empty. An omitted `--tools` is the CLI's full
    # default set, so "this stage may use nothing" has to be spelled out rather
    # than left off; the first version omitted it and got the opposite of what
    # it meant.
    argv += (
        ["--tools", *sorted(permissions.available_tools)]
        if permissions.available_tools
        else ["--tools", ""]
    )
    if permissions.auto_approve:
        argv += ["--allowedTools", *sorted(permissions.auto_approve)]
    if permissions.denied_tools:
        argv += ["--disallowedTools", *sorted(permissions.denied_tools)]
    if request.max_budget_usd is not None:
        argv += ["--max-budget-usd", str(request.max_budget_usd)]
    return argv


class ClaudeCliProvider:
    """Runs one stage. Decides nothing about what comes back."""

    name = "claude-cli"

    def run_stage(self, request: StageRequest) -> StageResult:
        started = time.monotonic()
        if not request.cwd.is_dir():
            # Checked rather than caught. A missing cwd raises
            # FileNotFoundError on POSIX -- indistinguishable from a missing
            # binary, and the first version reported it as one -- and
            # NotADirectoryError on Windows, which escaped uncaught.
            return self._failed(
                f"the stage was given {request.cwd} as its working directory "
                f"and that is not a directory, so nothing could be run there.",
                started,
            )
        try:
            argv = _argv(request)
        except PolicyError as exc:
            return self._failed(str(exc), started)
        too_long = self._argv_too_long(argv)
        if too_long is not None:
            return self._failed(too_long, started)

        before = sentinel.fingerprint(request.cwd)
        try:
            raw, err, code = self._invoke(argv, request)
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
                mutation=sentinel.assert_unchanged(request.cwd, before),
            )
        except OSError as exc:
            return self._failed(
                f"the `claude` CLI could not be started: "
                f"{type(exc).__name__}: {exc}",
                started,
            )
        mutation = sentinel.assert_unchanged(request.cwd, before)
        return self._interpret(raw, err, code, request, started, mutation)

    @staticmethod
    def _argv_too_long(argv: list[str]) -> str | None:
        if not _ENFORCE_ARGV_LIMIT:
            return None
        length = sum(len(part) + 3 for part in argv)
        if length <= _WINDOWS_ARGV_LIMIT:
            return None
        return (
            f"the command line for this stage is {length} characters and "
            f"Windows caps it at 32767. The stage schema goes inline and is "
            f"almost certainly what is oversized; shrink it rather than "
            f"reinstalling anything."
        )

    def _invoke(self, argv: list[str], request: StageRequest) -> tuple[str, str, int]:
        # Not a `with`. `Popen.__exit__` ends in an unbounded `wait()` that
        # defeats the timeout this call depends on; see `_subprocess.py`.
        proc = subprocess.Popen(
            argv,
            cwd=request.cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            encoding="utf-8",
            # A byte that is not valid UTF-8 kills the reader thread outright
            # without this, leaving stdout None with returncode 0 -- success by
            # the gate below, and a TypeError two frames later. Two call sites
            # in this repo have already shipped that defect. Decoding is half
            # the job: `_interpret` contains what survives it, because
            # `deps.py`'s own comment says porting the decode without the
            # containment just moves the crash from json.loads to sqlite.
            errors="surrogateescape",
            env=_child_env(),
            **new_group(),
        )
        try:
            # Text mode translates `\n` to os.linesep on write, so the same
            # prompt is different BYTES on the two CI legs -- which means the
            # Windows leg misses the Linux leg's prompt cache and pays for a
            # fresh prefix on every stage.
            if proc.stdin is not None:
                proc.stdin.reconfigure(newline="")
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
        mutation: str | None,
    ) -> StageResult:
        # Seeded, then reassigned once the envelope parses. `failed` closes
        # over this name and reads it at CALL time, so a failure raised before
        # the envelope is readable honestly reports zero turns.
        turns = 0

        def failed(reason: str, usage: Usage | None = None, **extra) -> StageResult:
            return StageResult(
                ok=False,
                data=None,
                raw=raw,
                usage=usage
                or Usage(wall_seconds=time.monotonic() - started, source="none"),
                error=reason,
                mutation=mutation,
                turns=turns,
                **extra,
            )

        try:
            envelope = json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            # A non-zero exit with unreadable stdout is the tool failing, not
            # the model declining, and the two get different sentences.
            if code != 0:
                return failed(
                    f"the `claude` CLI exited {code} without usable output: "
                    f"{err.strip() or 'no stderr'}"
                )
            return failed(
                "the `claude` CLI returned output that is not JSON, so the "
                "stage cannot be read. Refusing rather than guessing at it."
            )
        if not isinstance(envelope, dict):
            return failed(
                "the `claude` CLI returned JSON that is not an object, so it "
                "carries no envelope to read."
            )

        turns = _int(envelope.get("num_turns"))
        usage = _usage_from(envelope, time.monotonic() - started)
        denials = _denials_from(envelope)

        if code != 0:
            # A well-formed envelope on a non-zero exit is a CLI that flushed
            # its output and then died. The first version ignored the exit code
            # whenever stdout happened to parse, so it returned ok=True and
            # dropped stderr on the floor.
            return failed(
                f"the `claude` CLI exited {code} after writing its envelope, "
                f"so the run did not complete: {err.strip() or 'no stderr'}",
                usage,
                denials=denials,
            )

        if "is_error" not in envelope:
            # FAIL CLOSED, for the same reason as `_denials_from`. A missing key
            # read as success, so an envelope shape we do not recognise was
            # indistinguishable from one that reported no error.
            return failed(
                "the envelope carries no `is_error` field, so whether the stage "
                "succeeded cannot be read. Refusing rather than assuming it did.",
                usage,
                denials=denials,
            )
        if envelope.get("is_error"):
            return failed(self._reason(envelope), usage, denials=denials)

        data = envelope.get("structured_output")
        if not isinstance(data, dict):
            return failed(
                "the envelope carried no structured_output object, so the "
                "stage produced no payload the spine can act on.",
                usage,
                denials=denials,
            )
        if unstorable(data) or unstorable(raw):
            # Contained here rather than downstream. `deps.py` ported the
            # surrogateescape decode without this guard, and a bad byte reached
            # sqlite as a lone surrogate and killed the whole run from
            # `runner.upsert`, outside every per-detector try.
            return failed(
                "the stage payload contains text that is not encodable as "
                "UTF-8, so it cannot be stored or shown. Refusing here rather "
                "than failing the run when it reaches the database.",
                usage,
                denials=denials,
            )
        try:
            # Validated HERE as well as by the CLI. The CLI's own validation is
            # the model's side of the claim, and a model's self-assessment is
            # never trusted -- if its enforcement changes, the spine must not
            # silently start accepting a wider shape.
            jsonschema.validate(data, request.schema)
        except jsonschema.ValidationError as exc:
            return failed(
                f"the stage payload does not match its schema: {exc.message}",
                usage,
                denials=denials,
            )
        except jsonschema.SchemaError as exc:
            # OUR bug, not the model's, and it used to take the whole run down
            # from inside the one call that was supposed to be the safe check.
            return failed(
                f"the schema for stage {request.stage!r} is not a valid JSON "
                f"Schema, so nothing can be validated against it: {exc.message}",
                usage,
                denials=denials,
            )

        if denials:
            return failed(
                "the stage was refused "
                + ", ".join(sorted(set(denials)))
                + " and its answer is therefore built on less than it asked "
                "for. Refusing rather than reporting a clean success.",
                usage,
                denials=denials,
            )
        if mutation is not None:
            return failed(
                f"the stage is read-only and the worktree changed: {mutation}",
                usage,
                denials=denials,
            )
        return StageResult(
            ok=True,
            data=data,
            raw=raw,
            usage=usage,
            error=None,
            denials=denials,
            mutation=None,
            turns=turns,
        )

    @staticmethod
    def _reason(envelope: dict[str, Any]) -> str:
        """Why the CLI says it failed, most specific first.

        `errors` is in the chain because a real budget-exhausted envelope has NO
        `result` key at all and carries `errors: ["Reached maximum budget
        ($0.25)"]`. The first version's chain never consulted it, so the user
        was told `subtype 'error_max_budget_usd'`.
        """
        result = str(envelope.get("result") or "").strip()
        if result:
            return result
        errors = envelope.get("errors")
        if isinstance(errors, list) and errors:
            joined = "; ".join(str(item).strip() for item in errors if item)
            if joined:
                return joined
        elif isinstance(errors, str) and errors.strip():
            return errors.strip()
        status = str(envelope.get("api_error_status") or "").strip()
        return status or f"subtype {envelope.get('subtype')!r}"

    def _failed(
        self, message: str, started: float, mutation: str | None = None
    ) -> StageResult:
        """A failure still cost wall time, and usually money. Reporting zero
        usage for it would under-report every run that went wrong."""
        return StageResult(
            ok=False,
            data=None,
            raw="",
            usage=Usage(wall_seconds=time.monotonic() - started, source="none"),
            error=message,
            mutation=mutation,
        )
