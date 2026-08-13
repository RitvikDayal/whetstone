"""The Claude Code CLI subprocess contract, exercised for real.

Nothing here patches the function under test. The seam is `_CLAUDE_ARGV`, the
same shape `deps.py` adopted after every test monkeypatched `_run_pip_audit`
itself and four defects lived in that function at once. Popen, the encoding, the
argv construction, the envelope parsing and the schema validation all execute.

A script on PATH cannot shadow the binary on Windows: CreateProcess resolves
only `.exe` from PATH, so `claude.bat` is never found by `subprocess` without a
shell. Replacing the argv prefix is the portable equivalent.

WHAT THE PREVIOUS VERSION OF THIS FILE GOT WRONG, because it is the point of
the rewrite. Thirty-two tests were green over a provider that enforced no
permissions at all, and twelve of them were mutation-proven incapable of
failing. The two habits that produced that:

  1. Asserting the SHAPE of the argv instead of the EFFECT of the run. Every
     permission test checked that a flag appeared. The flag appeared. It was
     the wrong flag.
  2. Fixtures that agree with the code. `_SUCCESS_ENVELOPE` put identical
     values in `result` and `structured_output`, so a provider reading the
     wrong key passed; it put usage numbers exactly where the code looked, so
     a provider blind to `modelUsage` passed.

So: the fixture below puts DIFFERENT values in every pair of fields the
provider must choose between, and the fake writes into a directory that is not
the stage's cwd, so the sentinel has a clean worktree to watch.

Every fact these tests encode about the CLI came from running it. See
`artifacts/task3-cli-transcripts.md` in the vault for the transcripts.
"""

from __future__ import annotations

import base64
import itertools
import json
import sys
from pathlib import Path

import jsonschema
import pytest

from whetstone.policy.gate import PermissionSet
from whetstone.policy.profiles import profile_for
from whetstone.provider import claude_cli as claude_cli_module
from whetstone.provider import registry as registry_module
from whetstone.provider.base import ProviderError, StageRequest
from whetstone.provider.claude_cli import (
    ClaudeCliProvider,
    _float_or_none,
    _int,
    _usage_from,
)
from whetstone.provider.registry import available_providers, get_provider

# The envelope shape `--output-format json` really returns, trimmed to the keys
# the provider reads. Token numbers are the measured ones from the probe: the
# 4-versus-41036 gap is the whole reason Usage carries the cache fields.
#
# `result` and `structured_output` DISAGREE on purpose. They agree in reality,
# which is what let a provider reading `json.loads(envelope["result"])` stay
# green through a mutation test.
#
# `duration_ms` is absurd on purpose too, for the same reason: it is what a
# provider that took wall time from the payload would report, and it has to be
# distinguishable from a real measurement.
_SUCCESS_ENVELOPE = {
    "is_error": False,
    "subtype": "success",
    "result": '{"findings": ["THE RESULT FIELD, WHICH IS NOT THE PAYLOAD"]}',
    "structured_output": {"findings": []},
    "usage": {
        "input_tokens": 4,
        "output_tokens": 55,
        "cache_creation_input_tokens": 41036,
        "cache_read_input_tokens": 40993,
    },
    "total_cost_usd": 0.4322515,
    "duration_ms": 999_999_000,
    "num_turns": 3,
    "permission_denials": [],
    "api_error_status": None,
}

_FAKE_CLAUDE = '''
import base64, json, os, pathlib, sys

cfg = json.loads(pathlib.Path({config!r}).read_text(encoding="utf-8"))
# BINARY, not sys.stdin.read(). Text mode applies universal newlines and turns
# a received \\r\\n back into \\n, which made the provider's `newline=""`
# reconfigure untestable: dropping it changed nothing the fake could see. The
# whole claim is about the BYTES the two CI legs send.
stdin_bytes = sys.stdin.buffer.read()
pathlib.Path({record!r}).write_text(
    json.dumps({{
        "argv": sys.argv[1:],
        "stdin_b64": base64.b64encode(stdin_bytes).decode("ascii"),
        "cwd": os.getcwd(),
        # What the CHILD actually inherited. Asserting on `_child_env()` alone
        # tested the helper and not the wiring: deleting `env=` from the Popen
        # call left the helper correct, unused, and the suite green.
        "env": dict(os.environ),
    }}),
    encoding="utf-8",
)
for name, body in cfg.get("writes", {{}}).items():
    pathlib.Path(os.getcwd(), name).write_text(body, encoding="utf-8")
if cfg.get("sleep_seconds"):
    import time
    time.sleep(cfg["sleep_seconds"])
if cfg.get("stdout_b64") is not None:
    sys.stdout.buffer.write(base64.b64decode(cfg["stdout_b64"]))
    sys.stdout.buffer.flush()
else:
    sys.stdout.write(cfg["stdout"])
sys.stderr.write(cfg["stderr"])
sys.exit(cfg["returncode"])
'''


class _Recorder:
    """What the provider really sent.

    THIS CLASS HAD THE DEFECT IT WAS BUILT TO CATCH. `flag` and `variadic` used
    `argv.index(name)` -- the FIRST occurrence -- while a CLI reads the last. So
    appending `--tools Bash Edit Write Agent` to the end of the argv was
    invisible to every assertion in this file, and a mutation that did exactly
    that stayed green. Both lookups now refuse a repeated flag outright.
    """

    def __init__(self, record: Path) -> None:
        self._record = record

    def _read(self) -> dict:
        return json.loads(self._record.read_text(encoding="utf-8"))

    def argv(self) -> list[str]:
        return self._read()["argv"]

    def stdin_bytes(self) -> bytes:
        return base64.b64decode(self._read()["stdin_b64"])

    def env(self) -> dict[str, str]:
        """The environment the child really ran under."""
        return self._read()["env"]

    def stdin(self) -> str:
        return self.stdin_bytes().decode("utf-8")

    def _sole_index(self, name: str) -> int | None:
        argv = self.argv()
        found = [i for i, token in enumerate(argv) if token == name]
        assert len(found) <= 1, (
            f"{name} appears {len(found)} times. A CLI takes the LAST "
            f"occurrence, so a duplicate silently overrides everything this "
            f"test asserts: {argv}"
        )
        return found[0] if found else None

    def flag(self, name: str) -> str | None:
        """The single value following *name*, or None."""
        index = self._sole_index(name)
        return None if index is None else self.argv()[index + 1]

    def variadic(self, name: str) -> list[str] | None:
        """Every value following *name* up to the next flag.

        Exists because `--tools` and `--allowedTools` are variadic, and the
        superseded provider comma-joined them into one token -- which also
        destroyed the scoped form `Bash(uv run pytest -q)`, the only way to
        narrow Bash to a specific command.
        """
        index = self._sole_index(name)
        if index is None:
            return None
        values = []
        for token in self.argv()[index + 1 :]:
            if token.startswith("--"):
                break
            values.append(token)
        return values


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """Install a fake `claude` and control exactly what it emits.

    Each call gets its own config and record paths. They used to be three fixed
    filenames, so a test installing two fakes -- which the budget test does --
    held two recorders onto one file and could only ever see the last run.

    The fake's own files live in `_fake/`, NEVER in the directory a stage runs
    in. The sentinel watches that directory and correctly failed a stage when
    the test harness wrote its bookkeeping there.
    """
    scratch = tmp_path / "_fake"
    scratch.mkdir()
    counter = itertools.count()

    def _install(
        stdout: str | None = None,
        returncode: int = 0,
        stderr: str = "",
        stdout_bytes: bytes | None = None,
        writes: dict[str, str] | None = None,
        sleep_seconds: float = 0,
    ) -> _Recorder:
        index = next(counter)
        config = scratch / f"config-{index}.json"
        record = scratch / f"record-{index}.json"
        payload: dict[str, object] = {
            "stdout": json.dumps(_SUCCESS_ENVELOPE) if stdout is None else stdout,
            "stderr": stderr,
            "returncode": returncode,
            "writes": writes or {},
            "sleep_seconds": sleep_seconds,
            "stdout_b64": (
                base64.b64encode(stdout_bytes).decode("ascii")
                if stdout_bytes is not None
                else None
            ),
        }
        config.write_text(json.dumps(payload), encoding="utf-8")
        script = scratch / f"fake_claude-{index}.py"
        script.write_text(
            _FAKE_CLAUDE.format(config=str(config), record=str(record)),
            encoding="utf-8",
        )
        monkeypatch.setattr(
            claude_cli_module, "_CLAUDE_ARGV", (sys.executable, str(script))
        )
        return _Recorder(record)

    return _install


@pytest.fixture
def workdir(tmp_path):
    """Where a stage runs. Separate from the fake's own files so that anything
    the sentinel reports here came from the stage."""
    path = tmp_path / "work"
    path.mkdir()
    return path


@pytest.fixture
def argv_recording_claude(fake_claude):
    """A fake that succeeds, so a test can assert on what was sent."""
    return fake_claude()


def _perms(**overrides) -> PermissionSet:
    base = dict(
        available_tools=frozenset({"Read", "Grep", "Glob"}),
        auto_approve=frozenset({"Read", "Grep", "Glob"}),
        denied_tools=frozenset({"Edit", "Write", "NotebookEdit"}),
        bash_allowlist=frozenset(),
        read_denied=(".env*",),
        write_root=None,
    )
    base.update(overrides)
    return PermissionSet(**base)


def _request(cwd: Path, **overrides) -> StageRequest:
    base = dict(
        stage="hunt",
        prompt="find bugs",
        schema={"type": "object", "properties": {"findings": {"type": "array"}}},
        permissions=profile_for("hunt"),
        effort="medium",
        max_budget_usd=None,
        cwd=cwd,
    )
    base.update(overrides)
    return StageRequest(**base)


# --- the bound ----------------------------------------------------------------


# The exact argv each profile must produce, sorted, one entry per name.
#
# EVERY LIST IS PINNED EXACTLY, and `reproduce`/`falsify` are here because
# `hunt` cannot catch the defect that matters: its `available_tools` and
# `auto_approve` are the SAME SET, so swapping which one feeds which flag is
# invisible. Every provider test used `hunt`, and the swap survived -- the
# fixture agreeing with the code, which is the exact habit this file's
# docstring claims was eliminated.
# All three are identical since 2026-08-13: no M1a stage gets a shell. They are
# still enumerated separately rather than collapsed, because the property being
# pinned is that each STAGE produces the right argv -- and the day one of them
# diverges, this table is where it has to be said out loud.
_ARGV_BY_STAGE = {
    "hunt": (["Glob", "Grep", "Read"], ["Glob", "Grep", "Read"]),
    "reproduce": (["Glob", "Grep", "Read"], ["Glob", "Grep", "Read"]),
    "falsify": (["Glob", "Grep", "Read"], ["Glob", "Grep", "Read"]),
}


@pytest.mark.parametrize("stage", sorted(_ARGV_BY_STAGE))
def test_the_two_tool_lists_reach_the_argv_and_are_not_interchangeable(
    stage, workdir, argv_recording_claude
):
    """THE DEFECT THIS WHOLE REWRITE EXISTS FOR, and the test that failed to
    catch its second form.

    `--allowedTools` auto-approves; `--tools` bounds. The provider sent only the
    former, so every stage held the CLI's full default toolset -- Bash, Agent,
    TaskCreate and the rest -- and a reviewer drove a read-only stage into
    creating files and running `git init`.

    Order is asserted, not `sorted()`-ed on the way in: a frozenset iterates in
    hash order, so an unsorted argv differs per PYTHONHASHSEED. That defeats
    prompt caching for the same reason `_MODEL` is pinned, and reading the
    values back through `sorted()` hid it.
    """
    tools, approve = _ARGV_BY_STAGE[stage]
    ClaudeCliProvider().run_stage(
        _request(workdir, stage=stage, permissions=profile_for(stage))
    )
    recorder = argv_recording_claude
    assert recorder.variadic("--tools") == tools, (
        "--tools IS the bound; omitting it or filling it from auto_approve "
        "gives the stage the CLI's defaults"
    )
    assert recorder.variadic("--allowedTools") == approve
    assert recorder.variadic("--disallowedTools") == ["Edit", "NotebookEdit", "Write"]


def test_the_argv_is_built_from_the_permission_set_and_never_from_the_stage_name(
    workdir, argv_recording_claude
):
    """A mutation that widened `--tools` for any stage whose name is not "hunt"
    survived, because every test passes matching `stage=` and `permissions=`.

    The property is real and worth pinning on its own: `_argv` must read the
    PermissionSet it was handed. A provider that re-derives powers from the
    stage name has a second, unreviewed policy table hidden inside it, and the
    one in `profiles.py` stops being the audit surface it is supposed to be.
    """
    ClaudeCliProvider().run_stage(
        _request(workdir, stage="reproduce", permissions=profile_for("hunt"))
    )
    assert argv_recording_claude.variadic("--tools") == ["Glob", "Grep", "Read"]


def test_the_two_flags_are_fed_from_the_two_DIFFERENT_sets(
    workdir, argv_recording_claude
):
    """A SYNTHETIC PermissionSet, deliberately not one from `PROFILES`.

    Option A made all three profiles identical and read-only, which is the right
    security answer and which silently destroyed this file's ability to catch a
    swap: when `available_tools == auto_approve`, feeding each flag from the
    other frozenset produces a byte-identical argv. The mutation that started
    this whole rewrite went green again the moment the profiles converged.

    So the asymmetry has to be manufactured here. The property under test is a
    property of `_argv`, not of today's roster, and it must keep holding on the
    day M1b hands a stage a scoped `Bash(...)` approval.
    """
    perms = _perms(
        available_tools=frozenset({"Read", "Grep", "Glob"}),
        auto_approve=frozenset({"Read"}),
        denied_tools=frozenset({"Write"}),
    )
    ClaudeCliProvider().run_stage(_request(workdir, permissions=perms))
    recorder = argv_recording_claude
    assert recorder.variadic("--tools") == ["Glob", "Grep", "Read"]
    assert recorder.variadic("--allowedTools") == ["Read"]


def test_removing_any_granted_tool_changes_the_bound(workdir, argv_recording_claude):
    """`--tools` carries the whole set, so dropping one name has to show. The
    roster-based assertions could only ever catch the removal of a tool the
    roster actually has -- `available_tools - {"Bash"}` was a no-op once no
    profile carried Bash, which reads like a caught mutation and is not one."""
    perms = _perms(
        available_tools=frozenset({"Read", "Grep", "Glob"}),
        auto_approve=frozenset({"Read", "Grep", "Glob"}),
        denied_tools=frozenset({"Write"}),
    )
    ClaudeCliProvider().run_stage(_request(workdir, permissions=perms))
    assert argv_recording_claude.variadic("--tools") == ["Glob", "Grep", "Read"]


def test_no_flag_is_sent_twice(workdir, argv_recording_claude):
    """A CLI takes the LAST occurrence of a repeated flag. Appending a second
    `--tools Bash Edit Write Agent` to the end of the argv therefore restores
    the full write toolset, and every assertion in this file that reads the
    FIRST occurrence still passes."""
    ClaudeCliProvider().run_stage(_request(workdir))
    argv = argv_recording_claude.argv()
    flags = [token for token in argv if token.startswith("--")]
    assert len(flags) == len(set(flags)), f"a flag is repeated: {argv}"


def test_the_tool_lists_are_variadic_not_comma_joined(workdir, argv_recording_claude):
    """The superseded provider joined tool names with commas. That also breaks
    the scoped form `Bash(uv run pytest -q)`, which contains spaces -- so the
    one mechanism that could narrow Bash to a single command was unusable and
    nothing said so."""
    ClaudeCliProvider().run_stage(_request(workdir))
    for flag in ("--tools", "--allowedTools", "--disallowedTools"):
        values = argv_recording_claude.variadic(flag)
        assert values, flag
        assert len(values) > 1, f"{flag} arrived as one token: {values}"
        assert not any("," in value for value in values), f"{flag}: {values}"


def test_a_scoped_approval_survives_as_a_single_argv_entry(
    workdir, argv_recording_claude
):
    """`Bash(uv run pytest -q)` has to arrive as ONE token. Comma-joining split
    it into five, and splitting on spaces would too."""
    perms = _perms(
        available_tools=frozenset({"Read", "Bash"}),
        auto_approve=frozenset({"Read", "Bash(uv run pytest -q)"}),
        denied_tools=frozenset({"Write"}),
    )
    ClaudeCliProvider().run_stage(_request(workdir, permissions=perms))
    assert "Bash(uv run pytest -q)" in argv_recording_claude.argv()


def test_an_empty_available_set_still_sends_the_flag(workdir, argv_recording_claude):
    """Omitting `--tools` is the CLI's full default set, so 'this stage may use
    nothing' is the one thing that MUST be spelled out. The superseded provider
    omitted every permission flag for an empty PermissionSet, making the
    strictest policy expressible and the absence of any policy produce the same
    command line."""
    perms = _perms(available_tools=frozenset(), auto_approve=frozenset())
    ClaudeCliProvider().run_stage(_request(workdir, permissions=perms))
    assert argv_recording_claude.variadic("--tools") == [""]


def test_a_stage_with_no_policy_refuses_before_spawning(workdir, fake_claude):
    """`permissions=None` produced an unrestricted invocation. It now refuses,
    and refuses without starting the binary at all."""
    recorder = fake_claude()
    result = ClaudeCliProvider().run_stage(_request(workdir, permissions=None))
    assert result.ok is False
    assert "policy" in result.error
    with pytest.raises(OSError):
        recorder.argv()  # nothing ran, so there is no record to read


def test_the_denied_list_is_sent_as_well(workdir, argv_recording_claude):
    """Redundant with `--tools` by design: a name that is not available cannot
    be called anyway, so this only bites if a release changes what `--tools`
    means. The last time this module trusted one flag's meaning it was wrong."""
    ClaudeCliProvider().run_stage(_request(workdir))
    denied = argv_recording_claude.variadic("--disallowedTools")
    assert sorted(denied) == ["Edit", "NotebookEdit", "Write"]


# --- what else gets sent ------------------------------------------------------


def test_the_prompt_goes_on_stdin_not_argv(workdir, argv_recording_claude):
    """`--add-dir`, `--tools` and `--allowedTools` are variadic and swallow a
    trailing positional. Confirmed in `claude --help` for 2.1.224."""
    ClaudeCliProvider().run_stage(_request(workdir))
    assert "find bugs" not in argv_recording_claude.argv()
    assert argv_recording_claude.stdin() == "find bugs"


def test_the_schema_is_passed_inline_and_whole(workdir, argv_recording_claude):
    """`--json-schema` takes the schema as a JSON string, not a file path. The
    original plan specified a file and there is no such thing.

    The superseded version asserted two ABSENCES -- no `$schema`, no `title` --
    and a schema gutted to `{"type": "object"}` satisfied both. What matters is
    that the constraints survive, so this asserts the whole document.
    """
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "hunt",
        "type": "object",
        "required": ["findings"],
        "properties": {"findings": {"type": "array"}},
        "additionalProperties": False,
    }
    ClaudeCliProvider().run_stage(_request(workdir, schema=schema))
    sent = json.loads(argv_recording_claude.flag("--json-schema"))
    assert sent == {
        "type": "object",
        "required": ["findings"],
        "properties": {"findings": {"type": "array"}},
        "additionalProperties": False,
    }


def test_the_isolation_flags_are_sent_with_their_values(workdir, argv_recording_claude):
    """Measured: a trivial call inherited the operator's global config and spent
    41,036 cache-creation tokens and $0.43 before doing any work.
    `--setting-sources ""` cuts that to 12,365 and $0.08; the other two flags
    were chosen first, without measuring, and do almost nothing alone.

    The superseded version asserted that `--model` was PRESENT and never that
    it carried a value, so dropping the pinned model passed. `--add-dir` and
    `--effort` were droppable outright.
    """
    ClaudeCliProvider().run_stage(_request(workdir, effort="high"))
    recorder = argv_recording_claude
    assert recorder.variadic("--setting-sources") == [""]
    assert "--strict-mcp-config" in recorder.argv()
    assert "--no-session-persistence" in recorder.argv()
    assert recorder.flag("--model") == "sonnet", "an inherited model is not comparable"
    assert recorder.flag("--effort") == "high"
    assert recorder.flag("--add-dir") == str(workdir)


def test_no_m1a_stage_is_given_a_shell(workdir, argv_recording_claude):
    """The scope decision of 2026-08-13, asserted at the argv rather than only
    in `PROFILES`. An empty `bash_allowlist` bounds nothing -- the CLI
    auto-approves what its own classifier calls read-only and records nothing --
    so the only enforceable answer is that `Bash` never reaches `--tools`."""
    for stage in ("hunt", "reproduce", "falsify"):
        ClaudeCliProvider().run_stage(
            _request(workdir, stage=stage, permissions=profile_for(stage))
        )
        argv = argv_recording_claude.argv()
        assert "Bash" not in argv, stage
        assert "Agent" not in argv, stage
        assert "TaskCreate" not in argv, stage


def test_the_permission_mode_is_pinned_rather_than_inherited(
    workdir, argv_recording_claude
):
    """Every "unapproved means refused" property rested on the CLI's DEFAULT for
    this mode, which the argv never asserted -- the same shape as trusting a
    flag's meaning without sending it."""
    ClaudeCliProvider().run_stage(_request(workdir))
    assert argv_recording_claude.flag("--permission-mode") == "default"


_LEAKY_VARS = (
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL",
    "CLAUDE_CODE_SOMETHING",
    "HTTPS_PROXY",
    "HTTP_PROXY",
)


def test_the_subprocess_really_runs_under_the_scrubbed_environment(
    workdir, fake_claude, monkeypatch
):
    """ASSERTED ON THE CHILD, not on the helper.

    `Popen` was called with no `env=`, so the isolation claim covered settings
    files and nothing else: `ANTHROPIC_BASE_URL=http://127.0.0.1:1` reached the
    CLI and redirected the API endpoint, proven by a 45s hang against a 2s
    control. The stage prompt contains repo source, so that is a live route for
    it to leave for a third party.

    The first version of this test called `_child_env()` directly, which left
    deleting `env=` from the Popen call completely invisible -- a correct
    helper, unused, and a green suite. The fake now records the environment it
    was actually given.
    """
    for name in _LEAKY_VARS:
        monkeypatch.setenv(name, "should-not-travel")
    recorder = fake_claude()

    ClaudeCliProvider().run_stage(_request(workdir))

    child = recorder.env()
    for leaked in _LEAKY_VARS:
        assert leaked not in child, f"{leaked} reached the CLI: {sorted(child)}"
    assert "PATH" in child, "the CLI still has to be able to find its own runtime"


def test_the_allow_list_admits_nothing_it_was_not_asked_to(monkeypatch):
    """An allow-list, not a deny-list: a deny-list of a vendor's environment
    variables is out of date the day they add one."""
    for name in _LEAKY_VARS:
        monkeypatch.setenv(name, "x")
    monkeypatch.setenv("SOME_VENDOR_VARIABLE_INVENTED_TOMORROW", "x")
    unexpected = [
        name
        for name in claude_cli_module._child_env()
        if name.upper() not in claude_cli_module._ENV_ALLOWED
    ]
    assert not unexpected, unexpected


def test_the_stage_runs_headless_and_asks_for_an_envelope(
    workdir, argv_recording_claude
):
    """The two flags nothing asserted, because the fake ignores argv and so
    only an explicitly-named flag exists at all.

    Without `-p` the real binary enters interactive mode and every stage blocks
    until the 900s timeout. Without `--output-format json` stdout is prose and
    every stage fails to parse. Both were droppable.
    """
    ClaudeCliProvider().run_stage(_request(workdir))
    assert "-p" in argv_recording_claude.argv()
    assert argv_recording_claude.flag("--output-format") == "json"


def test_a_budget_is_sent_only_when_set(workdir, fake_claude):
    """Two fakes in one test. They used to share one record file, so the first
    recorder read the second run's argv."""
    bounded = fake_claude()
    ClaudeCliProvider().run_stage(_request(workdir, max_budget_usd=0.5))
    assert bounded.flag("--max-budget-usd") == "0.5"

    unbounded = fake_claude()
    ClaudeCliProvider().run_stage(_request(workdir))
    assert "--max-budget-usd" not in unbounded.argv()
    assert bounded.flag("--max-budget-usd") == "0.5", (
        "the first recorder must still see the first run -- one shared record "
        "file is what made this test unable to distinguish them"
    )


# --- what comes back ----------------------------------------------------------


def test_the_payload_comes_from_structured_output_not_result(
    workdir, argv_recording_claude
):
    """Both fields carry the answer in a real envelope, so the fixture makes
    them disagree. Reading `json.loads(envelope["result"])` was mutation-proven
    to leave the superseded test green."""
    result = ClaudeCliProvider().run_stage(_request(workdir))
    assert result.ok is True, result.error
    assert result.data == {"findings": []}


def test_usage_carries_the_cache_fields(workdir, argv_recording_claude):
    """input_tokens was 4 while cache_creation_input_tokens was 41,036. A budget
    reading only input_tokens under-reports by four orders of magnitude."""
    usage = ClaudeCliProvider().run_stage(_request(workdir)).usage
    assert usage.input_tokens == 4
    assert usage.cache_creation_input_tokens == 41036
    assert usage.cache_read_input_tokens == 40993
    assert usage.total_tokens == 4 + 55 + 41036 + 40993
    assert usage.source == "usage"


def test_model_usage_is_read_when_the_top_level_block_is_zero(workdir, fake_claude):
    """A real budget-exhausted envelope: `usage` all zeros, `modelUsage`
    reporting 47,661 cache-creation tokens. The provider read only `usage`, so a
    stage that burned 47k tokens reported a total of zero -- and the fixture had
    put the numbers where the code looked, so nothing caught it."""
    envelope = dict(
        _SUCCESS_ENVELOPE,
        usage={
            "input_tokens": 0,
            "output_tokens": 0,
            "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0,
        },
        modelUsage={
            "claude-sonnet-4-5": {
                "inputTokens": 12,
                "outputTokens": 30,
                "cacheCreationInputTokens": 47661,
                "cacheReadInputTokens": 100,
            }
        },
    )
    fake_claude(stdout=json.dumps(envelope))
    usage = ClaudeCliProvider().run_stage(_request(workdir)).usage
    assert usage.cache_creation_input_tokens == 47661
    assert usage.total_tokens == 12 + 30 + 47661 + 100
    assert usage.source == "modelUsage"


def test_a_nonsense_usage_block_reports_zero_rather_than_raising(workdir, fake_claude):
    """A `usage` that is not a mapping at all."""
    envelope = dict(_SUCCESS_ENVELOPE, usage="not a block", modelUsage=[1, 2])
    fake_claude(stdout=json.dumps(envelope))
    result = ClaudeCliProvider().run_stage(_request(workdir))
    assert result.ok is True, result.error
    assert result.usage.total_tokens == 0
    assert result.usage.source == "none"


# The coercion ladder below was DEAD CODE under test: a probe that made `_int`
# raise on anything other than int or None stayed green across the whole suite,
# because the only nonsense fixture short-circuits in `_tokens` before any
# individual value is read. Every one of these mutations reported wrong numbers
# silently rather than raising, which is the worse failure for a budget.
@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (4, 4),
        (0, 0),
        (-5, 0),  # a negative count is nonsense, and must not subtract
        (True, 0),  # bool is an int subclass and is never a token count
        (False, 0),
        (12.9, 12),
        (-1.5, 0),
        ("12", 12),
        ("12.5", 12),  # the ValueError the docstring names
        ("", 0),
        ("lots", 0),
        (None, 0),
        ({"nested": 1}, 0),  # the TypeError the docstring names
        ([1, 2], 0),
        # `json.loads` accepts bare Infinity / -Infinity / NaN by default, so
        # every one of these is reachable from a parseable envelope. `int(inf)`
        # raises OverflowError, which is neither ValueError nor TypeError, so it
        # escaped `_usage_from` -> `_interpret` -> `run_stage` and the provider
        # RAISED instead of returning a StageResult. The first version of this
        # parametrised list had fourteen cases and not one infinity.
        (float("inf"), 0),
        (float("-inf"), 0),
        (float("nan"), 0),
        ("Infinity", 0),
        ("-inf", 0),
        ("nan", 0),
        ("1e400", 0),  # overflows to inf inside float(), not a ValueError
    ],
)
def test_int_coerces_every_shape_a_model_runtime_can_produce(value, expected):
    assert _int(value) == expected


@pytest.mark.parametrize("nonfinite", ["Infinity", "-Infinity", "NaN"])
def test_a_nonfinite_token_count_does_not_take_the_run_down(
    nonfinite, workdir, fake_claude
):
    """End to end, because the unit case above proves `_int` and not the
    contract. A provider that raises has broken its only promise: it returns a
    StageResult, always. `json.loads` parses these without a flag."""
    body = json.dumps(_SUCCESS_ENVELOPE).replace(
        '"input_tokens": 4', f'"input_tokens": {nonfinite}'
    )
    assert json.loads(body), "the premise: this is a parseable envelope"
    fake_claude(stdout=body)
    result = ClaudeCliProvider().run_stage(_request(workdir))
    assert result.ok is True, result.error
    assert result.usage.input_tokens == 0


@pytest.mark.parametrize("nonfinite", ["Infinity", "NaN"])
def test_a_nonfinite_cost_is_unknown_rather_than_fatal(nonfinite, workdir, fake_claude):
    body = json.dumps(_SUCCESS_ENVELOPE).replace(
        '"total_cost_usd": 0.4322515', f'"total_cost_usd": {nonfinite}'
    )
    fake_claude(stdout=body)
    result = ClaudeCliProvider().run_stage(_request(workdir))
    assert result.ok is True, result.error
    assert result.usage.cost_usd is None, "an unpriceable stage costs unknown, not inf"


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0.43, 0.43),
        (1, 1.0),
        ("0.43", 0.43),  # a stringified cost must not become "no cost"
        ("free", None),
        (True, None),
        (None, None),
        ({}, None),
    ],
)
def test_float_or_none_distinguishes_unknown_from_zero(value, expected):
    assert _float_or_none(value) == expected


def test_model_usage_is_summed_across_models_not_overwritten():
    """One model in the fixture meant summing was never exercised, so replacing
    `totals[field] += count` with `=` survived. A stage that used two models
    would have reported only the last one."""
    envelope = {
        "modelUsage": {
            "claude-sonnet-4-5": {"inputTokens": 10, "cacheCreationInputTokens": 100},
            "claude-haiku-4-5": {"inputTokens": 3, "cacheCreationInputTokens": 7},
        }
    }
    usage = _usage_from(envelope, 1.0)
    assert usage.input_tokens == 13
    assert usage.cache_creation_input_tokens == 107


def test_a_field_wise_maximum_is_labelled_as_coming_from_both():
    """Neither block dominates: `usage` wins on output, `modelUsage` on cache
    creation. The `"usage+modelUsage"` label was never produced by any fixture,
    so hard-coding `source = "modelUsage"` passed."""
    envelope = {
        "usage": {"output_tokens": 900, "cache_creation_input_tokens": 1},
        "modelUsage": {"m": {"outputTokens": 2, "cacheCreationInputTokens": 500}},
    }
    usage = _usage_from(envelope, 1.0)
    assert usage.output_tokens == 900
    assert usage.cache_creation_input_tokens == 500
    assert usage.source == "usage+modelUsage"


def test_the_reported_cost_reaches_usage(workdir, argv_recording_claude):
    usage = ClaudeCliProvider().run_stage(_request(workdir)).usage
    assert usage.cost_usd == pytest.approx(0.4322515)


def test_wall_time_is_measured_by_us_not_taken_from_the_payload(
    workdir, argv_recording_claude
):
    """A claim with a physical referent is recomputed from the world.

    The fixture's `duration_ms` is 999,999,000 -- about eleven days. The
    superseded fixture used 4754, so feeding `wall_seconds` from the payload
    produced 4.75 and satisfied `0 < wall_seconds < 60` exactly as a real
    measurement would.
    """
    usage = ClaudeCliProvider().run_stage(_request(workdir)).usage
    assert 0 < usage.wall_seconds < 60


# --- refusals, mutations and failures ------------------------------------------


def test_a_refused_tool_is_not_a_clean_success(workdir, fake_claude):
    """`permission_denials` was never read, so a stage refused the tool it
    needed returned ok=True with error=None -- a path that declined to do the
    work and said nothing, which is the shape this repo bans everywhere."""
    envelope = dict(
        _SUCCESS_ENVELOPE,
        permission_denials=[{"tool_name": "Bash", "tool_input": {}}],
    )
    fake_claude(stdout=json.dumps(envelope))
    result = ClaudeCliProvider().run_stage(_request(workdir))
    assert result.ok is False
    assert result.denials == ("Bash",)
    assert "Bash" in result.error


def test_a_stage_that_writes_to_the_worktree_fails_even_when_nothing_was_refused(
    workdir, fake_claude
):
    """The whole reason `sentinel.py` exists.

    `permission_denials` is empty here, exactly as it was in the real run where
    a reviewer's stage created files: a tool that is simply absent from
    `--tools` is never refused, because there was no call to refuse. Absence is
    invisible to the CLI's own report and visible to the filesystem.
    """
    fake_claude(writes={"pwned.txt": "owned\n"})
    result = ClaudeCliProvider().run_stage(_request(workdir))
    assert result.denials == (), "the premise: the CLI reports nothing refused"
    assert result.ok is False
    assert result.mutation is not None
    assert "pwned.txt" in result.mutation
    assert (workdir / "pwned.txt").exists()


def test_output_violating_the_schema_fails(workdir, fake_claude):
    """Validated here as well as by the CLI. The CLI's structured_output is the
    model's side of the claim, and a model's self-assessment is never trusted.

    THE VIOLATION IS A WRONG TYPE IN A PRESENT FIELD, deliberately. The previous
    fixture used a missing required field, which a bare `{}` also violates -- so
    `jsonschema.validate({}, request.schema)` passed this test while validating
    something other than the payload. A payload whose only fault was a wrong
    type would then have been accepted and returned as `data`.
    """
    envelope = dict(_SUCCESS_ENVELOPE, structured_output={"findings": "not an array"})
    fake_claude(stdout=json.dumps(envelope))
    schema = {"type": "object", "properties": {"findings": {"type": "array"}}}
    assert jsonschema.Draft202012Validator(schema).is_valid({}), (
        "the premise: an empty object must SATISFY this schema, so a validator "
        "pointed at the wrong value cannot pass by accident"
    )
    result = ClaudeCliProvider().run_stage(_request(workdir, schema=schema))
    assert result.ok is False
    assert result.data is None
    assert "schema" in result.error.lower()


def test_a_non_object_payload_is_named_as_such(workdir, fake_claude):
    """`structured_output` arriving as a list is a different failure from a list
    that violates the schema, and the reader needs the first sentence. Relaxing
    the check to `data is None` let a list through to jsonschema and produced
    the wrong message."""
    envelope = dict(_SUCCESS_ENVELOPE, structured_output=["findings", "as", "a", "list"])
    fake_claude(stdout=json.dumps(envelope))
    result = ClaudeCliProvider().run_stage(_request(workdir))
    assert result.ok is False
    assert result.data is None
    assert "structured_output" in result.error


def test_an_invalid_schema_is_named_as_ours_rather_than_taking_the_run_down(
    workdir, argv_recording_claude
):
    """`jsonschema.SchemaError` was uncaught, so a malformed stage schema
    escaped from the one call that exists to be the safe check."""
    result = ClaudeCliProvider().run_stage(
        _request(workdir, schema={"type": "object", "required": "findings"})
    )
    assert result.ok is False
    assert "not a valid JSON Schema" in result.error


def test_unparseable_output_fails_rather_than_guessing(workdir, fake_claude):
    fake_claude(stdout="this is not json")
    result = ClaudeCliProvider().run_stage(_request(workdir))
    assert result.ok is False
    assert result.data is None
    assert "JSON" in result.error


def test_an_error_envelope_is_reported_even_on_exit_zero(workdir, fake_claude):
    """`is_error` lives in the payload and is not the exit code."""
    envelope = dict(
        _SUCCESS_ENVELOPE,
        is_error=True,
        subtype="error_during_execution",
        structured_output=None,
        result="I cannot help with that",
    )
    fake_claude(stdout=json.dumps(envelope), returncode=0)
    result = ClaudeCliProvider().run_stage(_request(workdir))
    assert result.ok is False
    assert result.data is None
    assert "cannot help" in result.error


def test_a_budget_exhausted_envelope_reports_the_cli_s_own_words(workdir, fake_claude):
    """Measured. It has NO `result` key at all and carries the message in
    `errors`, which the reason chain never consulted -- so the user was told
    `subtype 'error_max_budget_usd'` and had to guess."""
    envelope = {
        "is_error": True,
        "subtype": "error_max_budget_usd",
        "errors": ["Reached maximum budget ($0.25)"],
        "usage": {},
        "modelUsage": {"claude-sonnet-4-5": {"cacheCreationInputTokens": 47661}},
    }
    fake_claude(stdout=json.dumps(envelope))
    result = ClaudeCliProvider().run_stage(_request(workdir))
    assert result.ok is False
    assert "Reached maximum budget" in result.error
    assert result.usage.cache_creation_input_tokens == 47661, (
        "a budget-exhausted stage still cost 47k tokens and must not report zero"
    )


def test_a_non_zero_exit_is_a_failure_even_when_the_envelope_parses(
    workdir, fake_claude
):
    """A CLI killed after flushing its envelope. The exit code was consulted
    only when stdout failed to parse, so this returned ok=True with stderr
    discarded."""
    fake_claude(
        stdout=json.dumps(_SUCCESS_ENVELOPE), returncode=137, stderr="Killed"
    )
    result = ClaudeCliProvider().run_stage(_request(workdir))
    assert result.ok is False
    assert "137" in result.error
    assert "Killed" in result.error


def test_a_missing_binary_is_a_named_error(workdir, monkeypatch):
    monkeypatch.setattr(
        claude_cli_module, "_CLAUDE_ARGV", ("nosuchbinary-zz-whetstone",)
    )
    result = ClaudeCliProvider().run_stage(_request(workdir))
    assert result.ok is False
    assert "not installed" in result.error


def test_a_bad_working_directory_is_not_blamed_on_the_binary(tmp_path, fake_claude):
    """A missing cwd raises FileNotFoundError on POSIX -- caught by the
    missing-binary arm and reported as 'install Claude Code' -- and
    NotADirectoryError on Windows, which escaped uncaught. Both send the reader
    somewhere useless."""
    fake_claude()
    missing = tmp_path / "does" / "not" / "exist"
    result = ClaudeCliProvider().run_stage(_request(missing))
    assert result.ok is False
    assert "not a directory" in result.error
    assert "not installed" not in result.error


def test_a_file_used_as_a_working_directory_is_rejected_the_same_way(
    tmp_path, fake_claude
):
    fake_claude()
    target = tmp_path / "afile.txt"
    target.write_text("x", encoding="utf-8")
    result = ClaudeCliProvider().run_stage(_request(target))
    assert result.ok is False
    assert "not a directory" in result.error


def test_a_crash_is_distinguished_from_a_refusal(workdir, fake_claude):
    """A non-zero exit is not automatically the model declining."""
    fake_claude(stdout="", returncode=2, stderr="Segmentation fault")
    result = ClaudeCliProvider().run_stage(_request(workdir))
    assert result.ok is False
    assert "Segmentation fault" in result.error
    assert "exited 2" in result.error, "the exit code has to reach the reader"


def test_usage_is_recorded_even_on_failure(workdir, fake_claude):
    """A failed stage still cost wall time, and usually money."""
    fake_claude(stdout="not json")
    result = ClaudeCliProvider().run_stage(_request(workdir))
    assert result.ok is False
    assert result.usage.wall_seconds > 0


def test_non_utf8_bytes_after_the_envelope_are_refused_by_name(workdir, fake_claude):
    """Two locale-codec defects have already shipped in this repo. The
    superseded assertion here was `ok is False or data is not None`, which is a
    tautology -- one of the two holds for every possible StageResult, and
    reintroducing the locale-codec defect left it green."""
    fake_claude(
        stdout_bytes=json.dumps(_SUCCESS_ENVELOPE).encode("utf-8") + b"\xff\xfe"
    )
    result = ClaudeCliProvider().run_stage(_request(workdir))
    assert result.ok is False
    assert result.data is None
    assert "JSON" in result.error


def test_a_non_utf8_byte_inside_the_payload_is_contained_here(workdir, fake_claude):
    """Valid JSON whose CONTENT cannot be encoded back to UTF-8. `deps.py`
    ported the surrogateescape decode without the containment and a lone
    surrogate reached sqlite, killing the run from `runner.upsert` -- outside
    every per-detector guard."""
    body = json.dumps(_SUCCESS_ENVELOPE).encode("utf-8")
    poisoned = body.replace(b'"structured_output": {"findings": []}',
                            b'"structured_output": {"findings": ["\xff"]}')
    assert poisoned != body, "the fixture has to actually be poisoned"
    fake_claude(stdout_bytes=poisoned)
    result = ClaudeCliProvider().run_stage(
        _request(workdir, schema={"type": "object"})
    )
    assert result.ok is False
    assert result.data is None
    assert "UTF-8" in result.error


# --- the registry -------------------------------------------------------------


def test_registry_resolves_and_refuses():
    assert "claude-cli" in available_providers()
    assert get_provider("claude-cli").name == "claude-cli"
    with pytest.raises(ProviderError, match="nosuchprovider"):
        get_provider("nosuchprovider")


def test_a_plugin_cannot_shadow_the_builtin_provider():
    """`_register_builtins()` runs at import and plugins load later, so a plugin
    named `claude-cli` simply replaced the real provider and every stage ran
    through it with nothing saying so."""

    class _Impostor:
        name = "claude-cli"

        def run_stage(self, request):  # pragma: no cover - never reached
            raise AssertionError("the impostor must never be registered")

    with pytest.raises(ProviderError, match="built-in"):
        registry_module.register(_Impostor())
    assert get_provider("claude-cli").name == "claude-cli"
    assert isinstance(get_provider("claude-cli"), ClaudeCliProvider)


def test_a_plugin_load_failure_is_remembered_rather_than_degrading(monkeypatch):
    """The lens registry set its loaded flag before the load loop, so one failing
    plugin raised once and every later call returned early with that plugin
    quietly missing. This registry must not repeat it."""

    attempts: list[int] = []

    class _BrokenEntry:
        name = "broken"

        def load(self):
            attempts.append(1)
            raise RuntimeError("boom")

    monkeypatch.setattr(registry_module, "_LOADED_PLUGINS", False)
    monkeypatch.setattr(registry_module, "_LOAD_ERROR", None)
    monkeypatch.setattr(registry_module, "entry_points", lambda group: [_BrokenEntry()])

    with pytest.raises(ProviderError, match="broken"):
        registry_module.get_provider("claude-cli")
    with pytest.raises(ProviderError, match="broken"):
        registry_module.get_provider("claude-cli")

    # Raising twice is satisfied by two different mechanisms: remembering the
    # failure, or simply re-running the failing load. Both look identical from
    # outside, and asserting only the exception passed against either. The load
    # count is what separates them, and REMEMBERED is the one this module
    # claims -- a registry that retries a broken plugin on every lookup pays
    # its import cost forever.
    assert attempts == [1], f"the load was retried rather than remembered: {attempts}"


# --- paths that no test used to execute at all ---------------------------------


def test_a_timeout_kills_the_stage_and_still_reports_the_worktree(
    workdir, fake_claude, monkeypatch
):
    """The timeout arm never ran. Its body could be replaced with a bare
    `raise` -- taking the whole run down instead of reporting a failed stage --
    and nothing noticed. Nor did anything hold its `assert_unchanged` call: a
    stage that wrote a file and then hung reported no mutation."""
    # Five, not three. The fake pays a cold `sys.executable` start plus a file
    # write before the timeout can fire, and interpreter start reaches 1-2s on a
    # loaded windows-latest runner. It measured 3.17s here against a 3s bound.
    monkeypatch.setattr(claude_cli_module, "_TIMEOUT_SECONDS", 5)
    fake_claude(writes={"written-then-hung.txt": "x\n"}, sleep_seconds=30)

    result = ClaudeCliProvider().run_stage(_request(workdir))

    assert result.ok is False
    assert "did not finish within 5s" in result.error
    assert result.mutation is not None, (
        "a stage that mutated the worktree and then hung must still say so"
    )
    assert "written-then-hung.txt" in result.mutation


def test_a_spawn_failure_that_is_not_a_missing_binary_is_reported_as_itself(
    workdir, fake_claude, monkeypatch
):
    """The OSError arm never ran either. On Windows an oversized argv arrives
    here as errno 206, and before this arm existed it escaped uncaught."""
    fake_claude()

    def boom(*args, **kwargs):
        raise OSError(206, "The filename or extension is too long")

    monkeypatch.setattr(claude_cli_module.subprocess, "Popen", boom)
    result = ClaudeCliProvider().run_stage(_request(workdir))
    assert result.ok is False
    assert "could not be started" in result.error
    assert "too long" in result.error


def test_an_oversized_command_line_is_refused_before_spawning_on_every_leg(
    workdir, fake_claude, monkeypatch
):
    """`_argv_too_long` was gated on `os.name == "nt"` inline, so on the two
    Ubuntu legs the whole function was unreachable and a mutation to its
    comparison went red on Windows only. The gate is now a module constant the
    test can set, so both legs prove the same thing."""
    monkeypatch.setattr(claude_cli_module, "_ENFORCE_ARGV_LIMIT", True)
    fake_claude()
    huge = {"type": "object", "properties": {f"f{i}": {"type": "string"}
                                             for i in range(4000)}}
    result = ClaudeCliProvider().run_stage(_request(workdir, schema=huge))
    assert result.ok is False
    assert "32767" in result.error
    assert "not installed" not in result.error, (
        "reporting this as a missing binary sends the reader to reinstall "
        "Claude Code, which is exactly what the old FileNotFoundError arm did"
    )


def test_the_turn_count_is_recorded_but_not_judged(workdir, fake_claude):
    """A one-turn stage called no tool, which is what a FABRICATED answer looks
    like and what a blanket refusal looks like. Proven: a `hunt` stage asked for
    the installed git version "measured by running it" invented one --
    schema-valid, no denials, no mutation, `ok=True`. The sentinel structurally
    cannot see it, because a fabricated READ changes nothing on disk.

    Recorded and NOT judged here: invariant 2 says the provider decides nothing,
    so what counts as substantive is the lens's call.
    """
    fake_claude(stdout=json.dumps(dict(_SUCCESS_ENVELOPE, num_turns=1)))
    result = ClaudeCliProvider().run_stage(_request(workdir))
    assert result.ok is True, "the provider must not start judging substance"
    assert result.turns == 1


def test_an_envelope_with_no_is_error_field_is_refused(workdir, fake_claude):
    """A missing key read as success, so an envelope shape we do not recognise
    was indistinguishable from one reporting no error -- fail-open, in the
    module whose thesis is that this contract moves underneath it."""
    envelope = {k: v for k, v in _SUCCESS_ENVELOPE.items() if k != "is_error"}
    fake_claude(stdout=json.dumps(envelope))
    result = ClaudeCliProvider().run_stage(_request(workdir))
    assert result.ok is False
    assert "is_error" in result.error


def test_an_unreadable_denials_field_is_refused_rather_than_read_as_empty(
    workdir, fake_claude
):
    """`()` for a malformed field means "nothing was refused", which is a claim
    about a value that could not be read."""
    fake_claude(stdout=json.dumps(dict(_SUCCESS_ENVELOPE, permission_denials="oops")))
    result = ClaudeCliProvider().run_stage(_request(workdir))
    assert result.ok is False
    assert "unreadable" in result.error


def test_json_that_is_not_an_object_is_refused(workdir, fake_claude):
    """A top-level array parses fine and then fails with an AttributeError two
    frames later. The guard existed; nothing executed it."""
    fake_claude(stdout=json.dumps([1, 2, 3]))
    result = ClaudeCliProvider().run_stage(_request(workdir))
    assert result.ok is False
    assert "not an object" in result.error


def test_a_failure_before_the_subprocess_still_records_wall_time(workdir, monkeypatch):
    """`_failed` and `_interpret.failed` are two different constructors and only
    the second was covered, so `_failed` could report zero wall time for every
    run that failed early."""
    monkeypatch.setattr(
        claude_cli_module, "_CLAUDE_ARGV", ("nosuchbinary-zz-whetstone",)
    )
    usage = ClaudeCliProvider().run_stage(_request(workdir)).usage
    assert usage.wall_seconds > 0
    assert usage.source == "none"


def test_the_reason_chain_prefers_the_cli_s_own_result_text(workdir, fake_claude):
    """The two chain tests used disjoint envelopes -- one with `result` and no
    `errors`, one the reverse -- so the ORDER between them was never pinned and
    swapping it survived."""
    envelope = {
        "is_error": True,
        "subtype": "error_during_execution",
        "result": "THE RESULT TEXT",
        "errors": ["the errors array"],
        "api_error_status": "429",
    }
    fake_claude(stdout=json.dumps(envelope))
    result = ClaudeCliProvider().run_stage(_request(workdir))
    assert result.error == "THE RESULT TEXT"


def test_the_reason_chain_falls_back_to_the_api_error_status(workdir, fake_claude):
    """The third arm never ran."""
    envelope = {"is_error": True, "subtype": "error_x", "api_error_status": "429"}
    fake_claude(stdout=json.dumps(envelope))
    result = ClaudeCliProvider().run_stage(_request(workdir))
    assert result.error == "429"


def test_the_reason_chain_ends_at_the_subtype(workdir, fake_claude):
    envelope = {"is_error": True, "subtype": "error_unknown"}
    fake_claude(stdout=json.dumps(envelope))
    result = ClaudeCliProvider().run_stage(_request(workdir))
    assert "error_unknown" in result.error


@pytest.mark.parametrize(
    ("entry", "expected"),
    [
        ({"tool_name": "Bash", "tool_input": {}}, "Bash"),
        ({"tool": "Write"}, "Write"),
        ({"something_else": 1}, "unnamed tool"),
        ("Edit", "Edit"),
    ],
)
def test_every_shape_of_denial_entry_reaches_the_reader(
    entry, expected, workdir, fake_claude
):
    """One fixture, one shape, one entry. The `tool` fallback, the plain-string
    arm and the unnamed case were all droppable."""
    envelope = dict(_SUCCESS_ENVELOPE, permission_denials=[entry])
    fake_claude(stdout=json.dumps(envelope))
    result = ClaudeCliProvider().run_stage(_request(workdir))
    assert result.ok is False
    assert result.denials == (expected,)
    assert expected in result.error


def test_several_distinct_denials_are_all_named(workdir, fake_claude):
    """Every fixture had exactly one denial, so a message naming only
    `denials[0]` passed. A stage refused three different tools has to say so."""
    envelope = dict(
        _SUCCESS_ENVELOPE,
        permission_denials=[
            {"tool_name": "Bash"},
            {"tool_name": "Write"},
            {"tool_name": "Bash"},
        ],
    )
    fake_claude(stdout=json.dumps(envelope))
    result = ClaudeCliProvider().run_stage(_request(workdir))
    assert result.denials == ("Bash", "Write", "Bash")
    assert "Bash" in result.error
    assert "Write" in result.error


def test_the_mutation_reaches_the_error_sentence_and_not_only_the_field(
    workdir, fake_claude
):
    """`result.mutation` was asserted; `result.error` was not. A caller that
    prints the error would have shown a mutation failure with no detail."""
    fake_claude(writes={"evidence.txt": "x\n"})
    result = ClaudeCliProvider().run_stage(_request(workdir))
    assert result.ok is False
    assert "evidence.txt" in result.error


def test_a_json_escaped_surrogate_poisons_the_payload_but_not_the_raw_text(
    workdir, fake_claude
):
    """The two `unstorable` clauses were only ever tripped together, so either
    one alone was droppable.

    This is the case that separates them, and it is the ordinary route rather
    than an exotic one: `"\\ud800"` is six ASCII characters on the wire, so
    `raw` is clean UTF-8, and `json.loads` hands back a real lone surrogate in
    `data`. Model output reaches here this way.
    """
    # Built as WIRE TEXT, because `json.dumps` of a Python string containing a
    # backslash escapes the backslash -- which produces the literal ten
    # characters and no surrogate at all, and a test that passes for that
    # reason proves nothing.
    envelope = dict(_SUCCESS_ENVELOPE, structured_output={"findings": ["PLACEHOLDER"]})
    body = json.dumps(envelope).replace("PLACEHOLDER", "caf\\ud800e")
    assert body.isascii(), "the premise: raw must be clean, only data poisoned"
    assert "\ud800" in json.loads(body)["structured_output"]["findings"][0], (
        "the premise: json.loads must hand back a REAL lone surrogate"
    )
    fake_claude(stdout=body)
    result = ClaudeCliProvider().run_stage(
        _request(workdir, schema={"type": "object"})
    )
    assert result.ok is False
    assert "UTF-8" in result.error


def test_a_bad_byte_outside_the_payload_still_refuses(workdir, fake_claude):
    """The mirror image: `raw` carries a byte that is not valid UTF-8, in a
    field the payload never touches. Dropping `unstorable(raw)` would let this
    through with a clean `data`."""
    envelope = dict(_SUCCESS_ENVELOPE, subtype="success")
    body = json.dumps(envelope).encode("utf-8")
    poisoned = body.replace(b'"subtype": "success"', b'"subtype": "succe\xffs"')
    assert poisoned != body
    fake_claude(stdout_bytes=poisoned)
    result = ClaudeCliProvider().run_stage(
        _request(workdir, schema={"type": "object"})
    )
    assert result.ok is False
    assert "UTF-8" in result.error


def test_the_prompt_is_sent_with_unix_newlines_on_every_platform(
    workdir, argv_recording_claude
):
    """Text mode translates `\\n` to `os.linesep` on write, so without
    `reconfigure(newline="")` the two CI legs send DIFFERENT BYTES for the same
    prompt and the Windows leg misses the Linux leg's prompt cache.

    The fake used to read stdin in text mode, which translated `\\r\\n` back to
    `\\n` before recording it -- so the claim was untestable and dropping the
    reconfigure changed nothing observable.
    """
    prompt = "line one\nline two\nline three"
    ClaudeCliProvider().run_stage(_request(workdir, prompt=prompt))
    sent = argv_recording_claude.stdin_bytes()
    assert b"\r\n" not in sent, f"newline translation leaked into stdin: {sent!r}"
    assert sent == prompt.encode("utf-8")
