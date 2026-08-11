import sys
import time

from whetstone.config.model import (
    CommandsConfig,
    EnvironmentConfig,
    ProjectConfig,
    WhetstoneConfig,
)
from whetstone.doctor import run_command, run_doctor


def _cfg(**commands) -> WhetstoneConfig:
    return WhetstoneConfig(
        project=ProjectConfig(name="demo"),
        environment=EnvironmentConfig(commands=CommandsConfig(**commands)),
    )


def test_successful_command_passes(tmp_path):
    result = run_command("probe", f'"{sys.executable}" -c "pass"', tmp_path, 30)
    assert result.ok is True
    assert result.skipped is False


def test_failing_command_fails_with_output(tmp_path):
    result = run_command(
        "probe",
        f'"{sys.executable}" -c "import sys; sys.stderr.write(\'boom\'); sys.exit(3)"',
        tmp_path,
        30,
    )
    assert result.ok is False
    assert "boom" in result.detail or "3" in result.detail


def test_timeout_is_a_failure_not_a_hang(tmp_path):
    result = run_command(
        "probe", f'"{sys.executable}" -c "import time; time.sleep(5)"', tmp_path, 1
    )
    assert result.ok is False
    assert "timed out" in result.detail.lower()


def test_timeout_is_bounded_when_a_grandchild_holds_the_pipe(tmp_path):
    """`subprocess.run(timeout=...)` kills the direct child then, on Windows,
    calls `communicate()` a second time with no timeout at all to gather the
    rest -- and that call hangs forever if a grandchild the command spawned
    still holds the stdout/stderr pipes open. `pytest` and `npm test` both
    routinely spawn children. Measured against a naive implementation, a 1s
    timeout was still running the grandchild at 30s+."""
    script = tmp_path / "spawn_and_hang.py"
    script.write_text(
        "import subprocess, sys, time\n"
        "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        "time.sleep(60)\n",
        encoding="utf-8",
    )
    started = time.monotonic()
    result = run_command("probe", f'"{sys.executable}" "{script}"', tmp_path, 1)
    elapsed = time.monotonic() - started

    assert result.ok is False
    assert "timed out" in result.detail.lower()
    assert elapsed < 20, f"timeout did not bound the run: {elapsed:.1f}s"


def test_undeclared_commands_are_skipped_not_failed(tmp_path):
    results = run_doctor(_cfg(), tmp_path, tmp_path)
    by_name = {r.name: r for r in results}
    assert by_name["command: test"].skipped is True
    assert by_name["command: test"].ok is True


def test_declared_commands_are_actually_executed(tmp_path):
    cfg = _cfg(test=f'"{sys.executable}" -c "pass"')
    results = run_doctor(cfg, tmp_path, tmp_path)
    by_name = {r.name: r for r in results}
    assert by_name["command: test"].ok is True
    assert by_name["command: test"].skipped is False


def test_dev_command_is_not_executed(tmp_path):
    """`dev` starts a long-running server; doctor must not launch it."""
    cfg = _cfg(dev=f'"{sys.executable}" -c "import time; time.sleep(600)"')
    results = run_doctor(cfg, tmp_path, tmp_path)
    by_name = {r.name: r for r in results}
    assert by_name["command: dev"].skipped is True


def test_state_path_check_is_present(tmp_path):
    results = run_doctor(_cfg(), tmp_path, tmp_path)
    assert any(r.name == "state path" for r in results)
