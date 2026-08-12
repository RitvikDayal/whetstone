import contextlib
import subprocess
import sys
import time

from whetstone import _subprocess
from whetstone import doctor as doctor_module
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
    """The assertion here used to be `"boom" in detail or "3" in detail`, and
    `detail` opens with the command string itself -- which contains the literal
    `boom` and the literal `3`. Both operands were satisfied by the echo before
    the implementation contributed anything. Deleting the `tail` computation in
    `run_command` left it green. Assert against the detail with the echoed
    command removed, so only what the implementation ADDED can satisfy it."""
    command = (
        f'"{sys.executable}" -c "import sys; sys.stderr.write(\'kaboom\'); sys.exit(3)"'
    )
    result = run_command("probe", command, tmp_path, 30)
    assert result.ok is False
    reported = result.detail.replace(f"`{command}`", "")
    assert "exited 3" in reported, result.detail
    assert "kaboom" in reported, result.detail


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


def test_the_bound_still_holds_when_the_kill_does_not_take(tmp_path, monkeypatch):
    """The one case `REAP_SECONDS` exists for, measured end to end.

    `kill_and_reap` is bounded, and that was never the question. The question is
    whether `run_command` is, and it was not: the timeout branch RETURNS from
    inside `with subprocess.Popen(...)`, so `exc_type is None` and
    `Popen.__exit__` takes its ordinary path, which ends in a bare unbounded
    `self.wait()`. CPython 3.11 special-cases only KeyboardInterrupt there.

    Measured against the `with` form, kill neutered, timeout=2s,
    REAP_SECONDS=15, child sleeping 90s:

        t+ 0.0s  run_command ENTER
        t+ 2.0s  kill_and_reap ENTER
        t+17.0s  kill_and_reap EXIT     <- bounded exactly as designed
        t+90.1s  run_command RETURNED   <- Popen.__exit__'s bare self.wait()

    Neutering the kill is the point: a kill that takes hides the defect
    completely, because the child is already dead by the time __exit__ waits
    for it. That is why every existing timeout test passed over this.
    """
    spawned: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    class _RecordingPopen(real_popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            spawned.append(self)

    monkeypatch.setattr(subprocess, "Popen", _RecordingPopen)

    def _reap_without_killing(proc):
        # kill_tree replaced by a no-op. Everything else kill_and_reap does is
        # kept, including its bound, so what this measures is the caller.
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.communicate(timeout=2)

    monkeypatch.setattr(doctor_module, "kill_and_reap", _reap_without_killing)

    started = time.monotonic()
    try:
        result = run_command(
            "probe", f'"{sys.executable}" -c "import time; time.sleep(60)"', tmp_path, 1
        )
        elapsed = time.monotonic() - started
    finally:
        # A failure here must not leave a 60s sleeper behind.
        for proc in spawned:
            with contextlib.suppress(OSError, ValueError):
                proc.kill()

    assert result.ok is False
    assert "timed out" in result.detail.lower()
    assert elapsed < 20, (
        "run_command outlived its own bound; the unbounded wait is back: "
        f"{elapsed:.1f}s"
    )


def test_a_successful_command_leaves_no_open_pipes(tmp_path, monkeypatch):
    """Dropping the context manager must not drop the guarantee it was adopted
    for. `communicate()` closes the pipes on the ordinary path; this pins that
    the ordinary path still ends with them closed."""
    spawned: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    class _RecordingPopen(real_popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            spawned.append(self)

    monkeypatch.setattr(subprocess, "Popen", _RecordingPopen)

    result = run_command("probe", f'"{sys.executable}" -c "pass"', tmp_path, 30)

    assert result.ok is True
    assert spawned, "run_command did not spawn a process"
    proc = spawned[0]
    assert proc.stdout.closed and proc.stderr.closed
    assert proc.poll() is not None, "the child was left unreaped"


def _recording_reap(monkeypatch) -> list[bool]:
    """Wrap the real `kill_and_reap` so a test can see its drain-completed flag.

    The pipes are closed if and only if that drain completed -- see
    `_subprocess.kill_and_reap`. `run_command` discards the flag, so a test
    asserting closure unconditionally is really asserting that the kill won a
    race, and passes or fails on machine speed. Pinning the flag makes the
    branch explicit and makes a drain failure report itself. Wrapping rather
    than faking: the real kill, drain and `close_pipes` all still run.
    """
    drained: list[bool] = []

    def _wrapper(proc):
        result = _subprocess.kill_and_reap(proc)
        drained.append(result)
        return result

    monkeypatch.setattr(doctor_module, "kill_and_reap", _wrapper)
    return drained


def test_a_timed_out_command_leaves_no_open_pipes(tmp_path, monkeypatch):
    """Same guarantee on the path that does not go through communicate()'s own
    cleanup -- and it is a CONDITIONAL guarantee: closed if and only if the
    bounded drain completed. The command spawns no grandchild, so nothing but
    the direct child holds the write end and the drain is deterministic; the
    flag is asserted anyway so the day that stops being true this fails on the
    premise rather than flaking on the conclusion."""
    spawned: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    class _RecordingPopen(real_popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            spawned.append(self)

    monkeypatch.setattr(subprocess, "Popen", _RecordingPopen)
    drained = _recording_reap(monkeypatch)

    result = run_command(
        "probe", f'"{sys.executable}" -c "import time; time.sleep(30)"', tmp_path, 1
    )

    assert result.ok is False
    assert drained == [True], f"the drain did not complete ({drained})"
    proc = spawned[0]
    assert proc.stdout.closed and proc.stderr.closed


def test_an_interrupt_kills_the_command_and_closes_the_pipes(tmp_path, monkeypatch):
    """Ctrl-C mid-command must not leave it, or anything it spawned, running.

    Closure is conditional on the bounded drain completing, same as the timeout
    test above; no grandchild here, so the drain is deterministic, and the flag
    is asserted so it cannot quietly stop being.
    """
    spawned: list[subprocess.Popen] = []
    interrupted: list[bool] = []
    real_popen = subprocess.Popen

    class _InterruptingPopen(real_popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            spawned.append(self)

        def communicate(self, *args, **kwargs):
            # First read only: kill_tree runs taskkill through this same
            # patched name on Windows.
            if not interrupted:
                interrupted.append(True)
                raise KeyboardInterrupt
            return super().communicate(*args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", _InterruptingPopen)
    drained = _recording_reap(monkeypatch)

    try:
        try:
            run_command(
                "probe",
                f'"{sys.executable}" -c "import time; time.sleep(60)"',
                tmp_path,
                30,
            )
        except KeyboardInterrupt:
            pass
        else:
            raise AssertionError("the interrupt did not reach the caller")

        proc = spawned[0]
        assert proc.poll() is not None, "the command survived the interrupt"
        assert drained == [True], f"the drain did not complete ({drained})"
        assert proc.stdout.closed and proc.stderr.closed
    finally:
        for proc in spawned:
            with contextlib.suppress(OSError, ValueError):
                proc.kill()


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


def test_dev_command_is_not_executed(tmp_path, monkeypatch):
    """`dev` starts a long-running server; doctor must not launch it.

    `skipped is True` alone does not say that. A `run_doctor` that launched the
    command and then still reported `skipped=True` passed it; the only thing
    catching a real launch was the 600-second sleep turning the suite into a
    hang, which is a timing accident and not an assertion. Record the spawns.
    `run_doctor` reaches no other subprocess on this config -- the git check
    uses `shutil.which` and a `.exists()` -- so any spawn at all is `dev`.
    """
    spawned: list[subprocess.Popen] = []
    real_popen = subprocess.Popen

    class _RecordingPopen(real_popen):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            spawned.append(self)

    monkeypatch.setattr(subprocess, "Popen", _RecordingPopen)

    cfg = _cfg(dev=f'"{sys.executable}" -c "import time; time.sleep(600)"')
    results = run_doctor(cfg, tmp_path, tmp_path)
    by_name = {r.name: r for r in results}
    assert by_name["command: dev"].skipped is True
    assert not spawned, "doctor launched a process for a config declaring only `dev`"


def test_state_path_check_is_present(tmp_path):
    results = run_doctor(_cfg(), tmp_path, tmp_path)
    assert any(r.name == "state path" for r in results)
