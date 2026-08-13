"""The boundary around code Whetstone did not write.

TWO KINDS OF TEST HERE, and the difference matters.

The fail-closed tests run everywhere: no image, no Docker, a daemon that will
not answer, a daemon running the wrong kind of container. They are the ones
that matter most, because they are what stands between a model's output and a
machine with no container on it.

The BOUNDARY tests need a working Linux-container daemon and are skipped
without one. They are the only tests that prove the container actually confines
anything -- asserting the argv contains `--network none` proves we typed it,
not that it works, and this project has already shipped one permission model
whose tests asserted a flag reached the argv while the flag did the opposite of
what was believed. They run on the Ubuntu CI legs.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from conftest import docker_expected, docker_works, needs_docker
from whetstone import sandbox

_IMAGE = "python:3.11-slim"


@pytest.fixture(autouse=True)
def _forget_the_probe():
    """The daemon answer is cached per process; these tests change the world."""
    sandbox.reset_probe_cache()
    yield
    sandbox.reset_probe_cache()


# --- fail closed ----------------------------------------------------------------


@pytest.mark.parametrize("image", [None, "", "   "])
def test_no_configured_image_means_no_execution(image):
    """There is deliberately no default. An upgrade must never be the thing
    that grants arbitrary code execution."""
    blocked = sandbox.availability(image)
    assert blocked is not None
    assert "no sandbox image is configured" in blocked.reason


def test_missing_docker_means_no_execution(monkeypatch):
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: None)
    blocked = sandbox.availability(_IMAGE)
    assert blocked is not None
    assert "not installed" in blocked.reason


def test_a_daemon_that_will_not_answer_means_no_execution(monkeypatch):
    """Docker on PATH is not Docker running. The reason has to say which."""
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: "/usr/bin/docker")
    monkeypatch.setattr(
        sandbox,
        "run_argv",
        lambda *a, **k: sandbox.ShellResult(
            returncode=1, stdout="", stderr="cannot connect", timed_out=False
        ),
    )
    blocked = sandbox.availability(_IMAGE)
    assert blocked is not None
    assert "daemon did not answer" in blocked.reason
    assert "cannot connect" in blocked.reason


def test_a_windows_container_daemon_means_no_execution(monkeypatch):
    """MEASURED ON THE CI LEGS. The Windows runners answer `docker info`
    happily and then fail `docker run` with "Windows does not support
    PidsLimit". A daemon that cannot run our containers is an unavailable
    sandbox, and saying so here is the difference between a clean refusal and
    a stage dying at exit 125."""
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: "docker.exe")
    monkeypatch.setattr(
        sandbox,
        "run_argv",
        lambda *a, **k: sandbox.ShellResult(
            returncode=0, stdout="windows\n", stderr="", timed_out=False
        ),
    )
    blocked = sandbox.availability(_IMAGE)
    assert blocked is not None
    assert "windows containers" in blocked.reason


def test_the_daemon_is_probed_once_per_process(monkeypatch):
    """`reproduce` asks per candidate, and the answer cannot change during a
    run. Without the cache a fifty-candidate run pays fifty daemon round trips
    to learn the same thing."""
    calls: list[int] = []
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: "/usr/bin/docker")

    def counting(*_args, **_kwargs):
        calls.append(1)
        return sandbox.ShellResult(
            returncode=0, stdout="linux\n", stderr="", timed_out=False
        )

    monkeypatch.setattr(sandbox, "run_argv", counting)
    assert sandbox.availability(_IMAGE) is None
    assert sandbox.availability(_IMAGE) is None
    assert len(calls) == 1, calls


def test_every_reason_is_a_sentence_a_user_can_act_on():
    """A refusal that does not say what to do is a dead end. Each of these ends
    up in front of somebody wondering why their finding is capped."""
    for image in (None, ""):
        reason = sandbox.availability(image).reason
        assert reason.endswith(".")
        assert len(reason.split()) > 8


# --- the argv, which is necessary and not sufficient -----------------------------


def _argv(command: str = "pytest -q") -> list[str]:
    return sandbox.docker_argv(command, Path("/tmp/work"), _IMAGE, "n", {})


def test_the_argv_carries_every_flag_the_boundary_depends_on():
    """Necessary, NOT sufficient -- see the module docstring. The boundary
    tests below are what show these do anything."""
    argv = _argv()

    assert argv[:2] == ["docker", "run"]
    assert "--rm" in argv
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert argv[argv.index("--security-opt") + 1] == "no-new-privileges"
    # Without --memory-swap, --memory caps RAM and leaves swap unbounded.
    assert argv[argv.index("--memory-swap") + 1] == argv[argv.index("--memory") + 1]


def test_the_argv_ends_with_the_image_and_the_exact_command():
    """THE EXACT TAIL. `argv[-3:] == [image, "sh", "-lc"] or argv[-4] == image`
    was never true in its first clause, so it reduced to the second -- and that
    accepts an argv ending at `-lc` with no command at all."""
    assert _argv("pytest -q tests")[-4:] == [_IMAGE, "sh", "-lc", "pytest -q tests"]


def test_the_worktree_is_the_only_mount():
    """One `-v`. A second mount is a second place a reproduction can write, and
    the whole claim is that it has exactly one."""
    mounts = [_argv()[i + 1] for i, part in enumerate(_argv()) if part == "-v"]
    assert len(mounts) == 1
    assert mounts[0].endswith(f":{sandbox.CONTAINER_WORKDIR}")


def test_the_container_is_named_so_a_timeout_can_stop_it():
    """Killing the docker CLIENT does not stop the container: it keeps running
    against the mount, racing the sentinel with writes that arrive after the
    stage was declared timed out."""
    argv = sandbox.docker_argv("x", Path("/tmp/w"), _IMAGE, "my-name", {})
    assert argv[argv.index("--name") + 1] == "my-name"


def test_the_environment_goes_through_docker_not_the_command():
    """A leading `VAR=value` binds to one simple command, so a compound
    `test_command` like `cd sub && pytest` would apply it to `cd` alone."""
    argv = sandbox.docker_argv("x", Path("/tmp/w"), _IMAGE, "n", {"FOO": "bar"})
    assert argv[argv.index("-e") + 1] == "FOO=bar"
    assert "FOO=bar" not in argv[-1]


# --- the boundary itself ---------------------------------------------------------


@needs_docker
def test_the_container_has_no_network(tmp_path):
    """THE FLAG THAT MATTERS MOST, proven rather than asserted.

    `returncode != 0` was the first version, and it passes for any failure at
    all -- a missing image, a malformed argv, an OOM kill. It would have been
    green with the container never starting, leaving `--network none`
    unverified. The probe reports that it ran BEFORE it tries the socket, so
    "no route" and "did not run" are different observations.
    """
    probe = (
        "import socket\n"
        "print('CONTAINER-STARTED', flush=True)\n"
        "socket.setdefaulttimeout(5)\n"
        "try:\n"
        "    socket.socket().connect(('1.1.1.1', 53))\n"
        "    print('NETWORK-REACHED')\n"
        "except OSError as exc:\n"
        "    print('NETWORK-BLOCKED', type(exc).__name__)\n"
    )
    (tmp_path / "probe.py").write_text(probe, encoding="utf-8")

    result = sandbox.run_sandboxed("python probe.py", tmp_path, _IMAGE, timeout=180)

    assert "CONTAINER-STARTED" in result.output, (
        f"the container never ran, so the network was not tested: {result.output}"
    )
    assert "NETWORK-BLOCKED" in result.output, result.output
    assert "NETWORK-REACHED" not in result.output


@needs_docker
def test_a_write_ABOVE_the_worktree_cannot_reach_the_host(tmp_path):
    """Impossible rather than detected afterwards. The sentinel only ever looks
    inside `project_root`, and only once the run is over.

    Writing to `../escaped.txt` succeeds INSIDE the container -- it lands on the
    container's own root filesystem, thrown away with `--rm`. What matters is
    that the host's directory above the mount is untouched, because the mount is
    the only thing joining the two.
    """
    outside = tmp_path.parent / "escaped.txt"
    assert not outside.exists(), "the premise: nothing there before the run"

    (tmp_path / "escape.py").write_text(
        "open('../escaped.txt', 'w').write('x')\nprint('WROTE-ABOVE')\n",
        encoding="utf-8",
    )
    result = sandbox.run_sandboxed("python escape.py", tmp_path, _IMAGE, timeout=180)

    assert "WROTE-ABOVE" in result.output, f"the container never ran: {result.output}"
    assert not outside.exists(), "a write above the worktree reached the host"


@needs_docker
def test_the_worktree_is_writable_because_that_is_the_point(tmp_path):
    """The boundary has to still let the reproduction do its job."""
    result = sandbox.run_sandboxed(
        "python -c \"open('made_inside.txt','w').write('x')\"",
        tmp_path,
        _IMAGE,
        timeout=180,
    )
    assert result.returncode == 0, result.output
    assert (tmp_path / "made_inside.txt").exists()


@docker_expected
def test_docker_is_available_where_it_is_expected():
    """The skip above is load-bearing: without Docker every boundary test in
    this file disappears and the run still reports success.

    Gated on Linux CI rather than asserted everywhere, because Docker is
    genuinely optional on a laptop and the Windows runners default to Windows
    containers. On the Ubuntu legs it is expected, and its absence should be a
    failure rather than a quietly smaller test count.
    """
    assert docker_works(), (
        "no linux-container docker daemon on a Linux CI leg, so every sandbox "
        "boundary test was skipped and the container is unverified"
    )


def test_the_command_reaches_docker_without_a_host_shell_reparsing_it(monkeypatch):
    """MEASURED ON CI. The first version flattened the argv into a string --
    quoting only the parts containing a space, escaping nothing -- and handed it
    to a host shell. Every quote inside the command was then re-parsed, and it
    broke differently on the Ubuntu and Windows legs because they have different
    shells. That is the shape of every quoting defect: correct on the machine it
    was written on.

    The command must arrive at docker as ONE argv element, byte for byte.
    """
    seen: list[list[str]] = []
    monkeypatch.setattr(
        sandbox,
        "run_argv",
        lambda argv, *a, **k: seen.append(list(argv))
        or sandbox.ShellResult(returncode=0, stdout="", stderr="", timed_out=False),
    )
    command = 'python -m pytest -q "test file.py" --junit-xml="r.xml"'
    sandbox.run_sandboxed(command, Path("/tmp/w"), _IMAGE, timeout=60)

    assert seen, "docker was never invoked"
    assert seen[0][-1] == command, seen[0][-1]


def test_a_timed_out_container_is_removed(monkeypatch):
    """Killing the docker CLIENT does not stop the container. It keeps running
    against the mount, so writes can arrive after the stage was declared timed
    out and race the sentinel."""
    calls: list[list[str]] = []

    def fake(argv, *_a, **_k):
        calls.append(list(argv))
        timed_out = argv[1] == "run"
        return sandbox.ShellResult(
            returncode=-1 if timed_out else 0, stdout="", stderr="", timed_out=timed_out
        )

    monkeypatch.setattr(sandbox, "run_argv", fake)
    sandbox.run_sandboxed("sleep 999", Path("/tmp/w"), _IMAGE, timeout=1)

    assert len(calls) == 2, calls
    name = calls[0][calls[0].index("--name") + 1]
    assert calls[1][:3] == ["docker", "rm", "-f"]
    assert calls[1][3] == name, "a different container was removed"
