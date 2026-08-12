"""The Claude Code CLI subprocess contract, exercised for real.

Nothing here patches the function under test. The seam is `_CLAUDE_ARGV`, the
same shape `deps.py` adopted after every test monkeypatched `_run_pip_audit`
itself and four defects lived in that function at once. Popen, the encoding, the
argv construction, the envelope parsing and the schema validation all execute.

A script on PATH cannot shadow the binary on Windows: CreateProcess resolves
only `.exe` from PATH, so `claude.bat` is never found by `subprocess` without a
shell. Replacing the argv prefix is the portable equivalent.

Every fact these tests encode about the CLI came from running it. See
`artifacts/task3-cli-transcripts.md` in the vault for the transcripts.
"""

from __future__ import annotations

import base64
import json
import sys
from pathlib import Path

import pytest

from whetstone.policy.profiles import profile_for
from whetstone.provider import claude_cli as claude_cli_module
from whetstone.provider import registry as registry_module
from whetstone.provider.base import ProviderError, StageRequest
from whetstone.provider.claude_cli import ClaudeCliProvider
from whetstone.provider.registry import available_providers, get_provider

# The envelope shape `--output-format json` really returns, trimmed to the keys
# the provider reads. Token numbers are the measured ones from the probe: the
# 4-versus-41036 gap is the whole reason Usage carries the cache fields.
_SUCCESS_ENVELOPE = {
    "is_error": False,
    "subtype": "success",
    "result": '{"findings": []}',
    "structured_output": {"findings": []},
    "usage": {
        "input_tokens": 4,
        "output_tokens": 55,
        "cache_creation_input_tokens": 41036,
        "cache_read_input_tokens": 40993,
    },
    "total_cost_usd": 0.4322515,
    "duration_ms": 4754,
    "num_turns": 3,
    "permission_denials": [],
    "api_error_status": None,
}

_FAKE_CLAUDE = '''
import base64, json, pathlib, sys

cfg = json.loads(pathlib.Path({config!r}).read_text(encoding="utf-8"))
stdin_text = sys.stdin.read()
pathlib.Path({record!r}).write_text(
    json.dumps({{"argv": sys.argv[1:], "stdin": stdin_text}}), encoding="utf-8"
)
if cfg.get("stdout_b64") is not None:
    sys.stdout.buffer.write(base64.b64decode(cfg["stdout_b64"]))
    sys.stdout.buffer.flush()
else:
    sys.stdout.write(cfg["stdout"])
sys.stderr.write(cfg["stderr"])
sys.exit(cfg["returncode"])
'''


class _Recorder:
    def __init__(self, record: Path) -> None:
        self._record = record

    def _read(self) -> dict:
        return json.loads(self._record.read_text(encoding="utf-8"))

    def argv(self) -> list[str]:
        return self._read()["argv"]

    def stdin(self) -> str:
        return self._read()["stdin"]

    def flag(self, name: str) -> str | None:
        """The value following *name* in the recorded argv, or None."""
        argv = self.argv()
        return argv[argv.index(name) + 1] if name in argv else None


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """Install a fake `claude` and control exactly what it emits.

    Returns the installer; call it to (re)configure the fake, and read the
    recorder it returns for the argv and stdin the provider really sent.
    """

    def _install(
        stdout: str | None = None,
        returncode: int = 0,
        stderr: str = "",
        stdout_bytes: bytes | None = None,
    ) -> _Recorder:
        config = tmp_path / "fake_claude_config.json"
        record = tmp_path / "fake_claude_record.json"
        payload: dict[str, object] = {
            "stdout": json.dumps(_SUCCESS_ENVELOPE) if stdout is None else stdout,
            "stderr": stderr,
            "returncode": returncode,
            "stdout_b64": (
                base64.b64encode(stdout_bytes).decode("ascii")
                if stdout_bytes is not None
                else None
            ),
        }
        config.write_text(json.dumps(payload), encoding="utf-8")
        script = tmp_path / "fake_claude.py"
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
def argv_recording_claude(fake_claude):
    """A fake that succeeds, so a test can assert on what was sent."""
    return fake_claude()


def _request(tmp_path: Path, **overrides) -> StageRequest:
    base = dict(
        stage="hunt",
        prompt="find bugs",
        schema={"type": "object", "properties": {"findings": {"type": "array"}}},
        permissions=profile_for("hunt"),
        effort="medium",
        max_budget_usd=None,
        cwd=tmp_path,
    )
    base.update(overrides)
    return StageRequest(**base)


# --- what gets sent -----------------------------------------------------------


def test_the_prompt_goes_on_stdin_not_argv(tmp_path, argv_recording_claude):
    """`--add-dir` and `--allowedTools` are variadic and swallow a trailing
    positional. Confirmed in `claude --help` for 2.1.224."""
    ClaudeCliProvider().run_stage(_request(tmp_path))
    assert "find bugs" not in argv_recording_claude.argv()
    assert argv_recording_claude.stdin() == "find bugs"


def test_the_schema_is_passed_inline_without_its_meta_keys(
    tmp_path, argv_recording_claude
):
    """`--json-schema` takes the schema as a JSON string, not a file path.

    The original plan specified a file and there is no such thing. `$schema` and
    `title` are stripped because the CLI cannot resolve the 2020-12 meta-schema
    `$ref`.
    """
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "title": "hunt",
        "type": "object",
        "properties": {"findings": {"type": "array"}},
    }
    ClaudeCliProvider().run_stage(_request(tmp_path, schema=schema))
    sent = json.loads(argv_recording_claude.flag("--json-schema"))
    assert "$schema" not in sent
    assert "title" not in sent
    assert sent["type"] == "object"


def test_the_isolation_flags_are_sent(tmp_path, argv_recording_claude):
    """Measured: a trivial call inherited the operator's global config and spent
    41,036 cache-creation tokens before doing any work.

    Whetstone would pay that on every stage of every run, for tools the policy
    gate then denies. If these flags stop being sent, the cost measured in the
    artifacts file is not the cost production pays.
    """
    ClaudeCliProvider().run_stage(_request(tmp_path))
    argv = argv_recording_claude.argv()
    assert "--strict-mcp-config" in argv
    assert "--no-session-persistence" in argv
    assert "--model" in argv, "an inherited model makes two runs incomparable"


def test_the_permission_set_reaches_the_argv(tmp_path, argv_recording_claude):
    """Deny-by-default is enforced at the CLI, not requested in a prompt."""
    ClaudeCliProvider().run_stage(_request(tmp_path))
    denied = argv_recording_claude.flag("--disallowedTools")
    assert "Write" in denied
    assert "Edit" in denied


def test_a_budget_is_sent_only_when_set(tmp_path, fake_claude):
    bounded = fake_claude()
    ClaudeCliProvider().run_stage(_request(tmp_path, max_budget_usd=0.5))
    assert bounded.flag("--max-budget-usd") == "0.5"

    unbounded = fake_claude()
    ClaudeCliProvider().run_stage(_request(tmp_path))
    assert "--max-budget-usd" not in unbounded.argv()


# --- what comes back ----------------------------------------------------------


def test_structured_output_becomes_the_data(tmp_path, argv_recording_claude):
    """The payload is nested in the envelope, not the whole of stdout."""
    result = ClaudeCliProvider().run_stage(_request(tmp_path))
    assert result.ok is True
    assert result.data == {"findings": []}


def test_usage_carries_the_cache_fields(tmp_path, argv_recording_claude):
    """input_tokens was 4 while cache_creation_input_tokens was 41,036. A budget
    reading only input_tokens under-reports by four orders of magnitude."""
    usage = ClaudeCliProvider().run_stage(_request(tmp_path)).usage
    assert usage.input_tokens == 4
    assert usage.cache_creation_input_tokens == 41036
    assert usage.cache_read_input_tokens == 40993


def test_the_reported_cost_reaches_usage(tmp_path, argv_recording_claude):
    usage = ClaudeCliProvider().run_stage(_request(tmp_path)).usage
    assert usage.cost_usd == pytest.approx(0.4322515)


def test_wall_time_is_measured_by_us_not_taken_from_the_payload(
    tmp_path, argv_recording_claude
):
    """A claim with a physical referent is recomputed from the world. The
    envelope's own duration_ms is the tool describing itself."""
    usage = ClaudeCliProvider().run_stage(_request(tmp_path)).usage
    assert usage.wall_seconds > 0
    assert usage.wall_seconds < 60


def test_output_violating_the_schema_fails(tmp_path, fake_claude):
    """Validated here as well as by the CLI. The CLI's structured_output is the
    model's side of the claim, and a model's self-assessment is never trusted."""
    envelope = dict(_SUCCESS_ENVELOPE, structured_output={"wrong": "shape"})
    fake_claude(stdout=json.dumps(envelope))
    result = ClaudeCliProvider().run_stage(
        _request(
            tmp_path,
            schema={
                "type": "object",
                "required": ["findings"],
                "properties": {"findings": {"type": "array"}},
                "additionalProperties": False,
            },
        )
    )
    assert result.ok is False
    assert result.data is None
    assert "schema" in result.error.lower()


def test_unparseable_output_fails_rather_than_guessing(tmp_path, fake_claude):
    fake_claude(stdout="this is not json")
    result = ClaudeCliProvider().run_stage(_request(tmp_path))
    assert result.ok is False
    assert result.data is None
    assert "JSON" in result.error


def test_an_error_envelope_is_reported_even_on_exit_zero(tmp_path, fake_claude):
    """`is_error` lives in the payload and is not the exit code."""
    envelope = dict(
        _SUCCESS_ENVELOPE,
        is_error=True,
        subtype="error_during_execution",
        structured_output=None,
        result="I cannot help with that",
    )
    fake_claude(stdout=json.dumps(envelope), returncode=0)
    result = ClaudeCliProvider().run_stage(_request(tmp_path))
    assert result.ok is False
    assert result.data is None
    assert "cannot help" in result.error


def test_a_missing_binary_is_a_named_error(tmp_path, monkeypatch):
    monkeypatch.setattr(
        claude_cli_module, "_CLAUDE_ARGV", ("nosuchbinary-zz-whetstone",)
    )
    result = ClaudeCliProvider().run_stage(_request(tmp_path))
    assert result.ok is False
    assert "not installed" in result.error


def test_a_crash_is_distinguished_from_a_refusal(tmp_path, fake_claude):
    """A non-zero exit is not automatically the model declining."""
    fake_claude(stdout="", returncode=2, stderr="Segmentation fault")
    result = ClaudeCliProvider().run_stage(_request(tmp_path))
    assert result.ok is False
    assert "Segmentation fault" in result.error
    assert "exited 2" in result.error, "the exit code has to reach the reader"


def test_usage_is_recorded_even_on_failure(tmp_path, fake_claude):
    """A failed stage still cost wall time, and usually money."""
    fake_claude(stdout="not json")
    result = ClaudeCliProvider().run_stage(_request(tmp_path))
    assert result.ok is False
    assert result.usage.wall_seconds > 0


def test_non_utf8_output_does_not_crash(tmp_path, fake_claude):
    """Two locale-codec defects have already shipped in this repo."""
    fake_claude(stdout_bytes=json.dumps(_SUCCESS_ENVELOPE).encode("utf-8") + b"\xff\xfe")
    result = ClaudeCliProvider().run_stage(_request(tmp_path))
    assert result.ok is False or result.data is not None


# --- the registry -------------------------------------------------------------


def test_registry_resolves_and_refuses():
    assert "claude-cli" in available_providers()
    assert get_provider("claude-cli").name == "claude-cli"
    with pytest.raises(ProviderError, match="nosuchprovider"):
        get_provider("nosuchprovider")


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
