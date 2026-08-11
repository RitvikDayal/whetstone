"""The pip-audit subprocess contract, exercised for real.

Every other test of this detector replaces `_run_pip_audit` itself, so the
function had never executed under test and four defects lived in it at once.
Nothing here patches `_run_pip_audit`. The tests either run the installed
pip-audit or point `_PIP_AUDIT_ARGV` at a script whose output shape we control,
so Popen, the encoding, the timeout, the kill, and the return-code gate all run.

pip-audit itself is a declared dev dependency, so it is present whenever the
suite runs properly. Its ABSENCE fails rather than skips: a regression test that
silently never runs is the mechanism that let the ambient-environment defect
through three reviews. Only a genuine index or network failure skips, and
`_is_environment_failure` is what keeps that line from widening back out into
"anything that went wrong".
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest
from _pytest.outcomes import Failed, Skipped

from whetstone.lenses.base import RunContext
from whetstone.lenses.hygiene.detectors import deps as deps_module
from whetstone.lenses.hygiene.detectors.deps import DepsDetector

# Present in the environment the tests run in -- pip-audit is a dev dependency,
# so it shares this venv -- and never declared by the demo projects below. If
# one of these is flagged, the ambient interpreter was audited instead of the
# project, which is the exact defect this file exists to pin down.
#
# Deliberately excludes requests, urllib3, idna, certifi and packaging: pip-audit
# depends on those too, so they appear on both sides and prove nothing.
AMBIENT_ONLY = frozenset(
    {
        "pip-audit",
        "cyclonedx-python-lib",
        "boolean-py",
        "pip-api",
        "pytest",
        "ruff",
        "typer",
        "pydantic",
        "pathspec",
        "jinja2",
        "whetstone-cli",
    }
)

# pip-audit reaching neither PyPI nor OSV is a property of the host, not of
# Whetstone, and skipping is the honest response. Anything else -- a bad argv, a
# target that does not exist, an option the tool rejects -- is a Whetstone-side
# regression and must fail. Getting this line wrong turns the regression test
# for the Critical back into a test that quietly never runs.
#
# INVARIANT: every alternative below is a multi-word phrase or a distinctive
# compound identifier. The text being matched embeds the full argv, which
# embeds a filesystem path, so a bare word here is a false positive waiting for
# the right temp directory. That is not hypothetical -- an earlier version of
# this pattern had `\b50[234]\b`, which matches pytest's own `pytest-503`
# basetemp, and bare `ssl`, `proxy`, `connection` and `timeout`, all of which
# are ordinary path components. Any of them would have downgraded a genuine
# Whetstone regression to a silent skip. Keep the spaces.
_ENVIRONMENT_FAILURE = re.compile(
    r"timed out|read timed out|"
    r"connection (?:refused|reset|aborted|error)|"
    r"failed to establish a new connection|network is unreachable|"
    r"temporary failure in name resolution|getaddrinfo failed|"
    r"max retries exceeded|"
    r"certificate verify failed|sslcertverificationerror|ssl error|"
    r"proxy error|proxyerror|"
    r"5(?:02|03|04) server error|service unavailable|bad gateway|"
    r"gateway time-?out|temporarily unavailable|"
    r"no matching distribution found|could not find a version",
    re.IGNORECASE,
)


def _is_environment_failure(skip_text: str) -> bool:
    """True when a pip-audit failure is the host's fault, not Whetstone's."""
    return _ENVIRONMENT_FAILURE.search(skip_text) is not None


# A pin with a published advisory and a published fix. Old enough that the
# advisory is not going to be withdrawn.
VULNERABLE_PIN = "requests==2.19.0"


def _ctx(root: Path, **options) -> RunContext:
    return RunContext(
        project_root=root,
        state_root=root / "state",
        files=(),
        tier="quick",
        lens_options={"options": options},
        run_id="run-1",
    )


def _fake_tool(tmp_path: Path, body: str) -> tuple[str, ...]:
    """An argv prefix that runs *body* instead of the real pip-audit.

    A script on PATH cannot shadow the tool on Windows: CreateProcess resolves
    only `.exe` from PATH, so `pip-audit.bat` is never found. Replacing the
    argv prefix is the portable equivalent and still runs the whole subprocess
    path under test.
    """
    script = tmp_path / "fake_pip_audit.py"
    script.write_text(body, encoding="utf-8")
    return (sys.executable, str(script))


# One advisory, so a recording run still produces a Candidate to assert on.
_ONE_ADVISORY = (
    "{'dependencies': [{'name': 'seen', 'version': '1.0', 'vulns': "
    "[{'id': 'X', 'fix_versions': [], 'description': 'placeholder'}]}]}"
)


def _argv_recording_tool(
    tmp_path: Path, record: Path, payload: str = "{'dependencies': []}"
) -> tuple[str, ...]:
    """A fake pip-audit that writes its argv to *record* as a JSON list.

    JSON, not newline-joined text: the point of these tests is to assert on
    argv ELEMENTS rather than on a flattened string, and a format that can be
    flattened invites exactly the substring assertion that broke CI. An
    argument may contain a path separator, a space, or a hyphen; it is never
    equal to "-r" unless it IS "-r".
    """
    return _fake_tool(
        tmp_path,
        "import json, pathlib, sys\n"
        f"pathlib.Path(r'{record}').write_text("
        "json.dumps(sys.argv[1:]), encoding='utf-8')\n"
        f"print(json.dumps({payload}))\n",
    )


def _recorded_argv(record: Path) -> list[str]:
    return json.loads(record.read_text(encoding="utf-8"))


def _requires_real_pip_audit() -> None:
    """Fail, do not skip, when the tool is missing.

    pip-audit is a declared dev dependency (`[dependency-groups] dev`), so its
    absence is a broken environment, not a host limitation. Skipping here is
    precisely the mechanism that let the ambient-environment defect survive
    three review passes: the proof was inert and nothing said so.
    """
    assert shutil.which("pip-audit") is not None, (
        "pip-audit is not on PATH. It is a declared dev dependency; run "
        "`uv sync --all-groups`. This is a failure and not a skip on purpose -- "
        "a regression test that silently never runs is worse than no test."
    )


# Only two tests run the real pip-audit, and both are the proof of the
# ambient-environment Critical. On a runner that cannot reach PyPI or OSV they
# both skipped, the suite went green, and NOTHING said the proof had not run --
# the same inert-proof mechanism that let that defect survive three reviews,
# arrived at from a different direction. A host that genuinely has no index has
# to say so out loud, once, by setting this.
_OFFLINE_OPT_OUT = "WHETSTONE_ALLOW_OFFLINE_AUDIT"


def _audit_or_skip(ctx: RunContext):
    """Run the detector. Skip only for a genuine, declared environment failure."""
    found = list(DepsDetector().detect(ctx))
    failures = [
        s
        for s in ctx.skips
        if "pip-audit failed" in s or "produced no output" in s
    ]
    for failure in failures:
        if _is_environment_failure(failure):
            if os.environ.get(_OFFLINE_OPT_OUT) != "1":
                pytest.fail(
                    "pip-audit could not reach its index, so the regression "
                    "proof for the ambient-environment defect did not run. A "
                    "skip here is indistinguishable from a pass and that is "
                    f"the whole problem. Set {_OFFLINE_OPT_OUT}=1 to allow it "
                    f"deliberately:\n{failure}"
                )
            pytest.skip(f"pip-audit could not reach its index here: {failure}")
        pytest.fail(
            "pip-audit failed for a reason that is not a network or index "
            "problem, so this is a Whetstone-side regression rather than a host "
            f"limitation:\n{failure}"
        )
    return found


# --- finding 1: audit the project, not the ambient interpreter ---------------


def test_pyproject_project_is_audited_not_the_ambient_environment(tmp_path):
    _requires_real_pip_audit()
    (tmp_path / "pyproject.toml").write_text(
        "[project]\n"
        'name = "demo-vuln"\n'
        'version = "0.1.0"\n'
        'requires-python = ">=3.11"\n'
        f'dependencies = ["{VULNERABLE_PIN}"]\n'
        "\n[build-system]\n"
        'requires = ["hatchling"]\n'
        'build-backend = "hatchling.build"\n',
        encoding="utf-8",
    )
    ctx = _ctx(tmp_path)
    found = _audit_or_skip(ctx)

    flagged = {candidate.subject for candidate in found}
    assert "requests" in flagged, (
        "the project pins a package with a published advisory and it was not "
        f"flagged; flagged={sorted(flagged)} skips={ctx.skips}"
    )
    assert not (flagged & AMBIENT_ONLY), (
        "packages from the interpreter running pip-audit were reported as the "
        f"project's dependencies: {sorted(flagged & AMBIENT_ONLY)}"
    )


def test_requirements_only_project_is_audited_from_the_file(tmp_path):
    _requires_real_pip_audit()
    (tmp_path / "requirements.txt").write_text(
        f"{VULNERABLE_PIN}\n", encoding="utf-8"
    )
    ctx = _ctx(tmp_path)
    found = _audit_or_skip(ctx)

    flagged = {candidate.subject for candidate in found}
    assert "requests" in flagged, f"flagged={sorted(flagged)} skips={ctx.skips}"
    assert not (flagged & AMBIENT_ONLY)


def test_pyproject_without_a_project_table_skips_loudly(tmp_path):
    """A pyproject that is only tool config declares no dependencies. Falling
    back to the ambient environment is the bug; saying so is the fix."""
    (tmp_path / "pyproject.toml").write_text(
        "[tool.ruff]\nline-length = 100\n", encoding="utf-8"
    )
    ctx = _ctx(tmp_path)
    assert list(DepsDetector().detect(ctx)) == []
    assert any(
        "pyproject.toml" in skip and "project" in skip for skip in ctx.skips
    ), ctx.skips


def test_setup_cfg_only_project_skips_loudly(tmp_path):
    (tmp_path / "setup.cfg").write_text("[metadata]\nname = demo\n", encoding="utf-8")
    ctx = _ctx(tmp_path)
    assert list(DepsDetector().detect(ctx)) == []
    assert any("setup.cfg" in skip for skip in ctx.skips), ctx.skips


def test_pyproject_wins_over_requirements_when_both_declare(tmp_path, monkeypatch):
    """Which manifest was audited is a property of the ARGV, not of the prose.

    This used to substring-search the finding's `detail` for "-r". The detail
    embeds the project path, and GitHub's runner temp path
    (`/pytest-of-runner/pytest-0/...`) contains "-r" inside "of-runner", so all
    four CI legs failed on a test that passed here only because this machine's
    temp paths happen not to contain those two characters. A path can contain
    "-r"; an argv element is never equal to "-r" unless it IS the flag.
    """
    record = tmp_path / "argv.json"
    monkeypatch.setattr(
        deps_module,
        "_PIP_AUDIT_ARGV",
        _argv_recording_tool(tmp_path, record, _ONE_ADVISORY),
    )
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "demo"\nversion = "0.1.0"\ndependencies = []\n',
        encoding="utf-8",
    )
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")

    found = list(DepsDetector().detect(_ctx(tmp_path)))

    argv = _recorded_argv(record)
    assert "-r" not in argv, argv
    assert os.fspath(tmp_path) in argv, argv
    assert not any(Path(arg).name == "requirements.txt" for arg in argv), argv
    assert [c.evidence.data["audited"] for c in found] == ["pyproject.toml"]


# --- finding 2: a dependency pip-audit declined to audit ---------------------


def test_declined_dependency_is_reported_not_dropped(tmp_path, monkeypatch):
    """The real shape, copied from pip-audit 2.10 auditing an editable install:
    a `skip_reason` and no `vulns` key at all."""
    monkeypatch.setattr(
        deps_module,
        "_PIP_AUDIT_ARGV",
        _fake_tool(
            tmp_path,
            "import json\n"
            "print(json.dumps({'dependencies': [\n"
            "    {'name': 'demo-edit', 'skip_reason': 'Dependency not found on "
            "PyPI and could not be audited: demo-edit (0.1.0)'},\n"
            "    {'name': 'clean', 'version': '1.0', 'vulns': []},\n"
            "]}))\n",
        ),
    )
    (tmp_path / "requirements.txt").write_text("-e .\n", encoding="utf-8")
    ctx = _ctx(tmp_path)
    assert list(DepsDetector().detect(ctx)) == []
    assert any(
        "demo-edit" in skip and "not found on PyPI" in skip for skip in ctx.skips
    ), ctx.skips


# --- finding 3: non-UTF-8 output ---------------------------------------------


def test_non_utf8_output_skips_instead_of_raising(tmp_path, monkeypatch):
    """A byte the child emits that is not UTF-8 killed the reader thread and
    left stdout None, which json.loads turned into a bare TypeError that
    `except json.JSONDecodeError` does not catch."""
    monkeypatch.setattr(
        deps_module,
        "_PIP_AUDIT_ARGV",
        _fake_tool(
            tmp_path,
            "import sys\n"
            "sys.stdout.buffer.write(b'{\"dependencies\": [\\xff\\xfe]}')\n"
            "sys.stdout.buffer.flush()\n",
        ),
    )
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    ctx = _ctx(tmp_path)
    assert list(DepsDetector().detect(ctx)) == []
    assert ctx.skips, "non-UTF-8 output produced neither a finding nor a skip"


def test_empty_output_skips_instead_of_raising(tmp_path, monkeypatch):
    monkeypatch.setattr(
        deps_module, "_PIP_AUDIT_ARGV", _fake_tool(tmp_path, "pass\n")
    )
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    ctx = _ctx(tmp_path)
    assert list(DepsDetector().detect(ctx)) == []
    assert any("no output" in skip for skip in ctx.skips), ctx.skips


# --- finding 7: the timeout has to bound the run -----------------------------


def test_timeout_is_bounded_when_a_grandchild_holds_the_pipe(tmp_path, monkeypatch):
    """`subprocess.run(timeout=...)` kills the direct child then waits for EOF
    on pipes a surviving grandchild still holds. pip-audit shells out to pip,
    so the bound was not a bound: measured still running at 90s against a 5s
    timeout."""
    monkeypatch.setattr(deps_module, "_TIMEOUT_SECONDS", 2)
    monkeypatch.setattr(
        deps_module,
        "_PIP_AUDIT_ARGV",
        _fake_tool(
            tmp_path,
            "import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
            "time.sleep(120)\n",
        ),
    )
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    ctx = _ctx(tmp_path)

    started = time.monotonic()
    assert list(DepsDetector().detect(ctx)) == []
    elapsed = time.monotonic() - started

    assert elapsed < 45, f"timeout did not bound the run: {elapsed:.1f}s"
    # startswith, not `in`: this skip interpolates the TimeoutExpired message,
    # which carries the whole argv and therefore a filesystem path. The prefix
    # is ours and sits before anything interpolated.
    assert any(
        skip.startswith("hygiene/deps: pip-audit failed") for skip in ctx.skips
    ), ctx.skips


# --- finding 8: the rest of the subprocess contract --------------------------


def test_missing_tool_skips_loudly(tmp_path, monkeypatch):
    monkeypatch.setattr(
        deps_module, "_PIP_AUDIT_ARGV", ("whetstone-no-such-tool-9f3a",)
    )
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    ctx = _ctx(tmp_path)
    assert list(DepsDetector().detect(ctx)) == []
    assert any("not installed" in skip for skip in ctx.skips), ctx.skips


def test_nonzero_exit_skips_and_carries_stderr(tmp_path, monkeypatch):
    monkeypatch.setattr(
        deps_module,
        "_PIP_AUDIT_ARGV",
        _fake_tool(
            tmp_path,
            "import sys\nprint('index unreachable', file=sys.stderr)\nsys.exit(2)\n",
        ),
    )
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    ctx = _ctx(tmp_path)
    assert list(DepsDetector().detect(ctx)) == []
    assert any("index unreachable" in skip for skip in ctx.skips), ctx.skips


def test_exit_one_is_success_because_that_is_how_advisories_are_reported(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        deps_module,
        "_PIP_AUDIT_ARGV",
        _fake_tool(
            tmp_path,
            "import json, sys\n"
            "print(json.dumps({'dependencies': [{'name': 'requests', "
            "'version': '2.19.0', 'vulns': [{'id': 'PYSEC-2018-28', "
            "'fix_versions': ['2.20.0'], 'description': 'CRLF injection.'}]}]}))\n"
            "sys.exit(1)\n",
        ),
    )
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    ctx = _ctx(tmp_path)
    found = list(DepsDetector().detect(ctx))
    assert [c.rule_id for c in found] == ["PYSEC-2018-28"]
    assert ctx.skips == []


def test_unparseable_json_skips_loudly(tmp_path, monkeypatch):
    monkeypatch.setattr(
        deps_module,
        "_PIP_AUDIT_ARGV",
        _fake_tool(tmp_path, "print('this is not json')\n"),
    )
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    ctx = _ctx(tmp_path)
    assert list(DepsDetector().detect(ctx)) == []
    assert any("unparseable JSON" in skip for skip in ctx.skips), ctx.skips


def test_the_fake_tool_seam_actually_replaces_the_real_one(tmp_path, monkeypatch):
    """A seam that silently kept calling the real tool would make every test
    above prove nothing."""
    marker = tmp_path / "ran"
    monkeypatch.setattr(
        deps_module,
        "_PIP_AUDIT_ARGV",
        _fake_tool(
            tmp_path,
            "import json, pathlib\n"
            f"pathlib.Path(r'{marker}').write_text('yes', encoding='utf-8')\n"
            "print(json.dumps({'dependencies': []}))\n",
        ),
    )
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    assert list(DepsDetector().detect(_ctx(tmp_path))) == []
    assert marker.is_file(), "the fake pip-audit never ran"


def test_argv_carries_the_project_and_no_ambient_fallback(tmp_path, monkeypatch):
    """Argv-level proof of finding 1: the audit target must be in the argv.
    `cwd=project_root` alone changes nothing, because pip-audit takes its
    target as an argument and defaults to the ambient interpreter."""
    record = tmp_path / "argv.json"
    monkeypatch.setattr(
        deps_module, "_PIP_AUDIT_ARGV", _argv_recording_tool(tmp_path, record)
    )
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    list(DepsDetector().detect(_ctx(tmp_path)))
    argv = _recorded_argv(record)
    assert "-r" in argv, argv
    assert any(Path(arg).name == "requirements.txt" for arg in argv), argv


def test_run_pip_audit_actually_spawns_a_process(tmp_path, monkeypatch):
    """Guards the guard: if `_run_pip_audit` ever stops shelling out, these
    tests would keep passing while proving nothing about a subprocess.

    This used to assert `deps_module.subprocess is subprocess`, which proves
    the module has an import. Delete the whole body of `_run_pip_audit` and
    that still passed. Assert the call instead.
    """
    calls: list[list[str]] = []
    real_popen = subprocess.Popen

    class _RecordingPopen(real_popen):
        def __init__(self, argv, *args, **kwargs):
            calls.append(list(argv))
            super().__init__(argv, *args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _RecordingPopen)
    monkeypatch.setattr(
        deps_module, "_PIP_AUDIT_ARGV", _fake_tool(tmp_path, "print('{}')\n")
    )
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")

    list(DepsDetector().detect(_ctx(tmp_path)))

    assert calls, "_run_pip_audit did not spawn a process"
    assert any("--format" in argv and "json" in argv for argv in calls), calls


def test_an_interrupt_mid_read_kills_the_child_and_closes_the_pipes(
    tmp_path, monkeypatch
):
    """`except subprocess.TimeoutExpired` was the only recovery path, so any
    other exit from communicate() -- Ctrl-C being the ordinary one -- walked
    straight out leaving pip-audit and the pip it shelled out to alive with the
    pipes still open. The timeout is not the only way out of a read."""
    created: list[subprocess.Popen] = []
    interrupted: list[bool] = []
    real_popen = subprocess.Popen

    class _InterruptingPopen(real_popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            created.append(self)

        def communicate(self, *args, **kwargs):
            # First read only. `_subprocess.kill_tree` runs `taskkill` through
            # subprocess.run on Windows, which builds a Popen of its own
            # through this same patched name.
            if not interrupted:
                interrupted.append(True)
                raise KeyboardInterrupt
            return super().communicate(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _InterruptingPopen)
    monkeypatch.setattr(
        deps_module,
        "_PIP_AUDIT_ARGV",
        _fake_tool(
            tmp_path,
            "import subprocess, sys, time\n"
            "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
            "time.sleep(60)\n",
        ),
    )
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")

    try:
        with pytest.raises(KeyboardInterrupt):
            list(DepsDetector().detect(_ctx(tmp_path)))

        audit = created[0]
        assert audit.poll() is not None, "pip-audit survived the interrupt"
        assert audit.stdout.closed and audit.stderr.closed
    finally:
        # Belt and braces: a failure here must not leave a 60s sleeper behind.
        for proc in created:
            with contextlib.suppress(OSError, ValueError):
                proc.kill()


def test_a_failing_kill_does_not_replace_the_original_exception(
    tmp_path, monkeypatch
):
    """`_subprocess.kill_tree` runs on the way out of a failed read, and the caller
    re-raises the original exception immediately after it. An OSError escaping
    the final `proc.kill()` -- it sits in a `finally`, outside the suppression
    covering taskkill/killpg -- masks that exception with itself, and the
    bounded reap after it never runs either. The interrupt is what the user
    needs to see, not the cleanup's own complaint."""
    real_popen = subprocess.Popen
    interrupted: list[bool] = []
    killed: list[bool] = []

    class _HostilePopen(real_popen):
        def communicate(self, *args, **kwargs):
            if not interrupted:
                interrupted.append(True)
                raise KeyboardInterrupt
            return super().communicate(*args, **kwargs)

        def kill(self):
            # The shape Popen.kill() raises when the platform refuses for a
            # reason other than the already-dead race it suppresses itself.
            killed.append(True)
            super().kill()
            raise OSError(5, "Access is denied")

    monkeypatch.setattr(subprocess, "Popen", _HostilePopen)
    monkeypatch.setattr(
        deps_module, "_PIP_AUDIT_ARGV", _fake_tool(tmp_path, "import time\ntime.sleep(60)\n")
    )
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")

    # KeyboardInterrupt, not OSError. Anchored on the type, because the whole
    # point is which exception reaches the caller.
    with pytest.raises(KeyboardInterrupt):
        list(DepsDetector().detect(_ctx(tmp_path)))

    assert killed, "kill_tree never reached the direct-child kill"


# --- gate round 2: surrogates must not escape the detector -------------------
#
# `errors="surrogateescape"` stops a non-UTF-8 byte from killing the reader
# thread, and then leaves a lone surrogate in the decoded text. sqlite3 refuses
# to bind one, and `upsert` runs in runner.py OUTSIDE HygienePack's
# per-detector guard, so the whole run died with an unhandled
# UnicodeEncodeError. These tests drive `execute_run`, not the detector, because
# the detector alone never touches the store and cannot show the failure.


def _run_with_payload(tmp_path: Path, monkeypatch, payload: bytes):
    """Run a real execute_run against a fake pip-audit emitting *payload*."""
    from whetstone.config.model import LensConfig, ProjectConfig, WhetstoneConfig
    from whetstone.runner import execute_run
    from whetstone.store.db import connect

    script = tmp_path / "fake_bytes.py"
    script.write_text(
        f"import sys\nsys.stdout.buffer.write({payload!r})\n"
        "sys.stdout.buffer.flush()\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(
        deps_module, "_PIP_AUDIT_ARGV", (sys.executable, str(script))
    )
    monkeypatch.setattr("whetstone.runner.resolve_files", lambda *a, **k: ())
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")

    state = tmp_path / "state"
    state.mkdir(exist_ok=True)
    conn = connect(state)
    cfg = WhetstoneConfig(
        project=ProjectConfig(name="demo"),
        lenses={"hygiene": LensConfig(only=["deps"])},
    )
    result = execute_run(
        conn, cfg, tmp_path, state, tier="quick", changed_only=False
    )
    row = conn.execute("SELECT * FROM runs WHERE id = ?", (result.run_id,)).fetchone()
    return result, row, conn


def test_a_bad_byte_in_the_package_name_does_not_kill_the_run(tmp_path, monkeypatch):
    from whetstone.store.findings import list_findings

    payload = (
        b'{"dependencies":[{"name":"caf\xe9-pkg","version":"1.0","vulns":'
        b'[{"id":"X-1","fix_versions":[],"description":"plain"}]}]}'
    )
    result, row, conn = _run_with_payload(tmp_path, monkeypatch, payload)

    assert row["status"] == "complete"
    assert list_findings(conn) == []
    assert any(
        "not valid UTF-8" in skip and "NOT recorded" in skip
        for skip in result.skips
    ), result.skips


def test_a_bad_byte_in_the_description_still_records_the_advisory(
    tmp_path, monkeypatch
):
    """Identity is intact here, so the advisory is real and actionable.
    Discarding a genuine security finding over one bad byte in its prose is the
    wrong failure direction; the text is escaped and the substitution is
    reported."""
    from whetstone.store.findings import list_findings

    payload = (
        b'{"dependencies":[{"name":"plainpkg","version":"1.0","vulns":'
        b'[{"id":"X-2","fix_versions":[],"description":"caf\xe9 crash"}]}]}'
    )
    result, row, conn = _run_with_payload(tmp_path, monkeypatch, payload)

    assert row["status"] == "complete"
    findings = list_findings(conn)
    assert [f.rule_id for f in findings] == ["X-2"]
    assert findings[0].subject == "plainpkg"
    # The property that matters is that the text is encodable at all; the
    # escaped spelling is asserted without a backslash literal so this line
    # cannot itself be misread.
    assert findings[0].detail.encode("utf-8")
    assert "udce9" in findings[0].detail
    assert any("not verbatim" in skip for skip in result.skips), result.skips


def test_a_bad_byte_in_the_advisory_id_does_not_kill_the_run(tmp_path, monkeypatch):
    """The id is the finding's identity and feeds the dedupe key."""
    from whetstone.store.findings import list_findings

    payload = (
        b'{"dependencies":[{"name":"plainpkg","version":"1.0","vulns":'
        b'[{"id":"X-\xe9","fix_versions":[],"description":"plain"}]}]}'
    )
    result, row, conn = _run_with_payload(tmp_path, monkeypatch, payload)

    assert row["status"] == "complete"
    assert list_findings(conn) == []
    assert any("NOT recorded" in skip for skip in result.skips), result.skips


def test_a_bad_byte_in_a_declined_dependency_does_not_kill_the_run(
    tmp_path, monkeypatch
):
    payload = (
        b'{"dependencies":[{"name":"plainpkg","skip_reason":"could not audit '
        b'caf\xe9 (0.1)"}]}'
    )
    result, row, _ = _run_with_payload(tmp_path, monkeypatch, payload)

    assert row["status"] == "complete"
    assert any("declined to audit" in skip for skip in result.skips), result.skips


def test_a_lone_surrogate_escape_in_valid_json_is_contained(tmp_path, monkeypatch):
    r"""No byte is malformed here: the JSON carries an explicit \ud800 escape,
    which json.loads decodes into a lone surrogate. That is why the guard is
    wider than the resolver's surrogateescape-only range.

    Raw, so this docstring describes the escape instead of containing a lone
    surrogate that no UTF-8 stream can print. A report plugin or a failure
    banner rendering it would have raised UnicodeEncodeError out of the test
    file itself."""
    from whetstone.store.findings import list_findings

    payload = (
        rb'{"dependencies":[{"name":"bad\ud800name","version":"1.0","vulns":'
        rb'[{"id":"X-3","fix_versions":[],"description":"plain"}]}]}'
    )
    result, row, conn = _run_with_payload(tmp_path, monkeypatch, payload)

    assert row["status"] == "complete"
    assert list_findings(conn) == []
    assert any("not valid UTF-8" in skip for skip in result.skips), result.skips


def test_clean_output_records_no_encoding_skip(tmp_path, monkeypatch):
    """The guard must not fire on ordinary text, including non-ASCII that IS
    perfectly storable."""
    from whetstone.store.findings import list_findings

    payload = (
        '{"dependencies":[{"name":"café-pkg","version":"1.0","vulns":'
        '[{"id":"X-4","fix_versions":["2.0"],"description":"naïve parse"}]}]}'
    ).encode()
    result, row, conn = _run_with_payload(tmp_path, monkeypatch, payload)

    assert row["status"] == "complete"
    findings = list_findings(conn)
    assert [f.subject for f in findings] == ["café-pkg"]
    assert "naïve" in findings[0].detail
    assert not any(
        "UTF-8" in skip or "verbatim" in skip for skip in result.skips
    ), result.skips


# --- CodeRabbit round: the leaf types, not just the container types ----------
#
# `detect` validated that payload, dependencies, each dependency, vulns and
# each vuln were the right CONTAINER. The scalars pulled out of them were used
# as-is, and `_unstorable` returns False for everything that is neither str nor
# sequence -- so `{"name": 42}` passed every guard, reached `Candidate.subject`,
# and died in `upsert`. That call site is in runner.py, outside HygienePack's
# per-detector guard, so the run ended `failed` with no skip line: the same
# path the surrogate comment in deps.py describes, arrived at by type rather
# than by encoding. Issue #14 is the general fix at Candidate construction.


def test_a_non_text_package_name_does_not_kill_the_run(tmp_path, monkeypatch):
    from whetstone.store.findings import list_findings

    payload = (
        b'{"dependencies":[{"name":42,"version":"1.0","vulns":'
        b'[{"id":"X-5","fix_versions":[],"description":"plain"}]}]}'
    )
    result, row, conn = _run_with_payload(tmp_path, monkeypatch, payload)

    assert row["status"] == "complete"
    assert list_findings(conn) == []
    assert any(
        "not text" in skip and "NOT recorded" in skip for skip in result.skips
    ), result.skips


def test_a_non_text_advisory_id_does_not_kill_the_run(tmp_path, monkeypatch):
    """A nested object where the id belongs. json.dumps in the dedupe key
    swallows it happily; sqlite does not."""
    from whetstone.store.findings import list_findings

    payload = (
        b'{"dependencies":[{"name":"plainpkg","version":"1.0","vulns":'
        b'[{"id":{"nested":1},"fix_versions":[],"description":"plain"}]}]}'
    )
    result, row, conn = _run_with_payload(tmp_path, monkeypatch, payload)

    assert row["status"] == "complete"
    assert list_findings(conn) == []
    assert any(
        "not text" in skip and "NOT recorded" in skip for skip in result.skips
    ), result.skips


def test_bare_string_fix_versions_is_refused_not_rendered_per_character(
    tmp_path, monkeypatch
):
    """`', '.join("2.0")` renders `2, ., 0`, and evidence.data holds a string
    where every consumer expects a list. `_unstorable` accepted both."""
    from whetstone.store.findings import list_findings

    payload = (
        b'{"dependencies":[{"name":"plainpkg","version":"1.0","vulns":'
        b'[{"id":"X-6","fix_versions":"2.0","description":"plain"}]}]}'
    )
    result, row, conn = _run_with_payload(tmp_path, monkeypatch, payload)

    assert row["status"] == "complete"
    assert list_findings(conn) == []
    assert any("fix_versions" in skip for skip in result.skips), result.skips


def test_a_non_text_description_still_records_the_advisory(tmp_path, monkeypatch):
    """Identity is intact, so the advisory is real. Prose is not what the user
    acts on -- same split as the bad-byte case -- but the loss is reported."""
    from whetstone.store.findings import list_findings

    payload = (
        b'{"dependencies":[{"name":"plainpkg","version":"1.0","vulns":'
        b'[{"id":"X-7","fix_versions":["2.0"],"description":{"text":"nested"}}]}]}'
    )
    result, row, conn = _run_with_payload(tmp_path, monkeypatch, payload)

    assert row["status"] == "complete"
    assert [f.rule_id for f in list_findings(conn)] == ["X-7"]
    assert any(
        "description that was not text" in skip for skip in result.skips
    ), result.skips


def test_well_typed_output_records_no_leaf_type_skip(tmp_path, monkeypatch):
    """The counterweight: the guard must not fire on the ordinary shape, or it
    becomes a line on every run and nobody reads the list."""
    from whetstone.store.findings import list_findings

    payload = (
        b'{"dependencies":[{"name":"plainpkg","version":"1.0","vulns":'
        b'[{"id":"X-8","fix_versions":["2.0"],"description":"plain"}]}]}'
    )
    result, row, conn = _run_with_payload(tmp_path, monkeypatch, payload)

    assert row["status"] == "complete"
    findings = list_findings(conn)
    assert [f.rule_id for f in findings] == ["X-8"]
    assert "Fixed in 2.0." in findings[0].detail
    assert not any("not text" in skip for skip in result.skips), result.skips


# --- gate round 2: the skip/fail split, and pip-audit's availability ---------


def test_pip_audit_is_a_declared_dev_dependency():
    """The regression tests for the Critical run the real tool. If it is not
    installed they skip, all four CI legs go green having proved nothing, and
    the suite reports a smaller number that nobody reads. Pinning it in the dev
    group is what makes those tests actually execute."""
    import tomllib

    root = Path(__file__).resolve().parents[2]
    with (root / "pyproject.toml").open("rb") as handle:
        data = tomllib.load(handle)
    dev = data["dependency-groups"]["dev"]
    assert any(
        str(entry).replace(" ", "").startswith("pip-audit") for entry in dev
    ), f"pip-audit missing from the dev dependency group: {dev}"
    assert shutil.which("pip-audit"), "declared in pyproject but not installed"


@pytest.mark.parametrize(
    "text",
    [
        "hygiene/deps: pip-audit failed (Command ... timed out after 120 seconds)",
        "hygiene/deps: pip-audit failed (Max retries exceeded with url: /simple/)",
        "hygiene/deps: pip-audit failed (Temporary failure in name resolution)",
        "hygiene/deps: pip-audit failed (getaddrinfo failed)",
        "hygiene/deps: pip-audit failed (503 Server Error: Service Unavailable)",
        "hygiene/deps: pip-audit failed (SSLCertVerificationError)",
    ],
)
def test_network_failures_are_classified_as_environment(text):
    assert _is_environment_failure(text)


@pytest.mark.parametrize(
    "text",
    [
        # The exact shapes a Whetstone-side argv regression produces.
        "hygiene/deps: pip-audit failed (couldn't find a supported project file in .)",
        "hygiene/deps: pip-audit failed (pyproject file pyproject.toml does not "
        "contain `project` section)",
        "hygiene/deps: pip-audit failed (unrecognized arguments: --bogus)",
        "hygiene/deps: pip-audit failed (exit 2)",
        "hygiene/deps: pip-audit produced no output while auditing pyproject.toml",
    ],
)
def test_whetstone_side_failures_are_not_classified_as_environment(text):
    """A regression must fail the suite, not be absorbed as 'offline'."""
    assert not _is_environment_failure(text)


def test_a_regressed_audit_target_fails_rather_than_skips(tmp_path, monkeypatch):
    """End-to-end proof of the split: a fake tool reproducing the argv
    regression must make _audit_or_skip fail, not skip."""
    monkeypatch.setattr(
        deps_module,
        "_PIP_AUDIT_ARGV",
        _fake_tool(
            tmp_path,
            "import sys\n"
            "print(\"couldn't find a supported project file in .\", file=sys.stderr)\n"
            "sys.exit(2)\n",
        ),
    )
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    with pytest.raises(Failed, match="Whetstone-side regression"):
        _audit_or_skip(_ctx(tmp_path))


def _offline_tool(tmp_path: Path) -> tuple[str, ...]:
    return _fake_tool(
        tmp_path,
        "import sys\n"
        "print('Max retries exceeded with url: /simple/requests/', "
        "file=sys.stderr)\n"
        "sys.exit(2)\n",
    )


def test_a_network_failure_skips_only_when_the_offline_opt_out_is_declared(
    tmp_path, monkeypatch
):
    monkeypatch.setenv(_OFFLINE_OPT_OUT, "1")
    monkeypatch.setattr(deps_module, "_PIP_AUDIT_ARGV", _offline_tool(tmp_path))
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    with pytest.raises(Skipped, match="could not reach its index"):
        _audit_or_skip(_ctx(tmp_path))


def test_an_undeclared_offline_run_fails_rather_than_skipping(tmp_path, monkeypatch):
    """The two real-tool tests are the entire proof of the ambient-environment
    Critical. An offline runner made both skip, all four legs went green, and
    nothing recorded that the proof was inert. A skip that can silently become
    permanent on a host is not a passing test."""
    monkeypatch.delenv(_OFFLINE_OPT_OUT, raising=False)
    monkeypatch.setattr(deps_module, "_PIP_AUDIT_ARGV", _offline_tool(tmp_path))
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    with pytest.raises(Failed, match="regression proof for the ambient"):
        _audit_or_skip(_ctx(tmp_path))


def test_the_opt_out_must_be_exactly_one(tmp_path, monkeypatch):
    """"0", "false", or an empty value are how people turn a flag OFF. Any of
    them re-enabling the silent skip would put the hole straight back."""
    monkeypatch.setenv(_OFFLINE_OPT_OUT, "0")
    monkeypatch.setattr(deps_module, "_PIP_AUDIT_ARGV", _offline_tool(tmp_path))
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    # Anchored to the offline message, not to `Failed`. `_audit_or_skip` has a
    # second pytest.fail for a failure that is NOT classified as environmental,
    # so a bare `raises(Failed)` would still pass if `_is_environment_failure`
    # stopped matching the retry message `_offline_tool` prints -- and would
    # then prove nothing about the opt-out value. The guard going quiet is the
    # same failure this guard exists to prevent, one layer up.
    with pytest.raises(Failed, match="regression proof for the ambient"):
        _audit_or_skip(_ctx(tmp_path))


# --- cosmetic: an unparseable manifest is not a missing table ---------------


def test_unparseable_pyproject_says_so_rather_than_blaming_the_table(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[project\nname = broken", encoding="utf-8"
    )
    ctx = _ctx(tmp_path)
    assert list(DepsDetector().detect(ctx)) == []
    assert any("could not be parsed as TOML" in skip for skip in ctx.skips), ctx.skips
    assert not any("no [project] table" in skip for skip in ctx.skips), ctx.skips


def test_an_unparseable_pyproject_still_falls_through_to_requirements(tmp_path):
    """A broken pyproject must not stop a requirements.txt from being audited."""
    (tmp_path / "pyproject.toml").write_text("[project\nbroken", encoding="utf-8")
    (tmp_path / "requirements.txt").write_text("requests\n", encoding="utf-8")
    plan = deps_module._plan_audit(tmp_path)
    assert not isinstance(plan, str)
    assert plan.source == "requirements.txt"


# --- gate round 3: assertions must not depend on the shape of a temp path ----


@pytest.mark.parametrize(
    "text",
    [
        # pytest's own basetemp counter reaches 503 on any long-lived machine.
        # An earlier classifier matched \b50[234]\b and would have called this
        # a network failure, silently skipping a real regression.
        "hygiene/deps: pip-audit failed (Command '['pip-audit', '-r', "
        "'/tmp/pytest-of-runner/pytest-503/test_x0/requirements.txt']' exit 2)",
        # Ordinary directory names that used to match bare alternatives.
        "hygiene/deps: pip-audit failed (exit 2) auditing "
        r"C:\src\ssl-proxy-connection-timeout\project",
        "hygiene/deps: pip-audit failed (couldn't find a supported project "
        "file in /home/runner/work/network-certificate-ssl/proj)",
    ],
)
def test_a_path_shaped_string_does_not_masquerade_as_a_network_failure(text):
    """The classifier reads a string that embeds the argv, and the argv embeds
    a path. A bare word in that pattern is a false positive waiting for the
    right directory name, and a false positive here downgrades a Whetstone
    regression to a skip."""
    assert not _is_environment_failure(text)


def test_no_assertion_in_this_file_substring_searches_a_hyphen_flag():
    """Guards the fix rather than the symptom.

    `assert "-r" not in <string>` is the defect that reddened all four CI legs:
    a two-character flag searched inside prose that embeds a temp path. Flags
    are argv ELEMENTS. If one shows up in an `in` comparison against something
    that is not obviously a list, it is the same bug again.
    """
    source = Path(__file__).read_text(encoding="utf-8")
    # Real assert statements only. Prose that quotes the defect -- this
    # docstring, for one -- is not the defect.
    offenders = [
        f"{number}: {line.strip()}"
        for number, line in enumerate(source.splitlines(), start=1)
        if line.lstrip().startswith("assert ")
        and re.search(r"""["']-[a-zA-Z]["']\s+(?:not\s+)?in\s+(?!argv)""", line)
    ]
    assert not offenders, (
        "a command-line flag is being searched for inside something that is "
        "not the recorded argv list:\n" + "\n".join(offenders)
    )
