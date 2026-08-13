"""The boundary around code Whetstone did not write.

TWO KINDS OF TEST HERE, and the difference matters.

The fail-closed tests run everywhere: no image, no Docker, no daemon. They are
the ones that matter most, because they are what stands between a model's
output and a machine with no container on it.

The BOUNDARY tests need a working Docker and are skipped without one. They are
the only tests that prove the container actually confines anything -- asserting
the argv contains `--network none` proves we typed it, not that it works, and
this project has already shipped one permission model whose tests asserted the
flag reached the argv while the flag did the opposite of what was believed.
They run on the Ubuntu CI legs.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from whetstone import sandbox

_IMAGE = "python:3.11-slim"


def _docker_works() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        return (
            subprocess.run(
                ["docker", "info"], capture_output=True, timeout=30
            ).returncode
            == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


needs_docker = pytest.mark.skipif(
    not _docker_works(), reason="docker is unavailable, so the boundary cannot be run"
)


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
        "run_shell",
        lambda *a, **k: sandbox.ShellResult(
            returncode=1, stdout="", stderr="cannot connect", timed_out=False
        ),
    )
    blocked = sandbox.availability(_IMAGE)
    assert blocked is not None
    assert "daemon did not answer" in blocked.reason
    assert "cannot connect" in blocked.reason


def test_every_reason_is_a_sentence_a_user_can_act_on():
    """A refusal that does not say what to do is a dead end. Each of these ends
    up in front of somebody wondering why their finding is capped."""
    for image in (None, ""):
        reason = sandbox.availability(image).reason
        assert reason.endswith(".")
        assert len(reason.split()) > 8


# --- the argv, which is necessary and not sufficient -----------------------------


def test_the_argv_carries_every_flag_the_boundary_depends_on():
    """Necessary, NOT sufficient -- see the module docstring. The boundary
    tests below are what show these do anything."""
    argv = sandbox.docker_argv("pytest -q", Path("/tmp/work"), _IMAGE)

    assert argv[:2] == ["docker", "run"]
    assert "--rm" in argv
    assert argv[argv.index("--network") + 1] == "none"
    assert argv[argv.index("--cap-drop") + 1] == "ALL"
    assert "no-new-privileges" in argv
    assert argv[-3:] == [_IMAGE, "sh", "-lc"] or argv[-4] == _IMAGE


def test_the_worktree_is_the_only_mount():
    """One `-v`. A second mount is a second place a reproduction can write, and
    the whole claim is that it has exactly one."""
    argv = sandbox.docker_argv("pytest -q", Path("/tmp/work"), _IMAGE)
    mounts = [argv[i + 1] for i, part in enumerate(argv) if part == "-v"]
    assert len(mounts) == 1
    assert mounts[0].endswith(f":{sandbox.CONTAINER_WORKDIR}")


# --- the boundary itself ---------------------------------------------------------


@needs_docker
def test_the_container_has_no_network(tmp_path):
    """THE FLAG THAT MATTERS MOST, proven rather than asserted.

    No network is why a reproduction cannot push, deploy, or send the
    repository anywhere -- and none of that depends on us enumerating commands
    to forbid.
    """
    probe = (
        "python -c \"import socket;"
        "socket.setdefaulttimeout(5);"
        "socket.socket().connect(('1.1.1.1',53))\""
    )
    result = sandbox.run_sandboxed(probe, tmp_path, _IMAGE, timeout=120)
    assert result.returncode != 0, "the container reached the network"


@needs_docker
def test_a_write_ABOVE_the_worktree_cannot_reach_the_host(tmp_path):
    """Impossible rather than detected afterwards. The sentinel only ever looks
    inside `project_root`, and only once the run is over.

    Writing to `../escaped.txt` succeeds INSIDE the container -- it lands on
    the container's own root filesystem, which is thrown away with `--rm`. What
    matters is that the host's directory above the mount is untouched, because
    the mount is the only thing joining the two.
    """
    outside = tmp_path.parent / "escaped.txt"
    assert not outside.exists(), "the premise: nothing there before the run"

    sandbox.run_sandboxed(
        "python -c \"open('../escaped.txt','w').write('x')\"",
        tmp_path,
        _IMAGE,
        timeout=120,
    )
    assert not outside.exists(), "a write above the worktree reached the host"


@needs_docker
def test_the_worktree_is_writable_because_that_is_the_point(tmp_path):
    """The boundary has to still let the reproduction do its job."""
    result = sandbox.run_sandboxed(
        "python -c \"open('made_inside.txt','w').write('x')\"",
        tmp_path,
        _IMAGE,
        timeout=120,
    )
    assert result.returncode == 0, result.output
    assert (tmp_path / "made_inside.txt").exists()


@pytest.mark.skipif(
    not (sys.platform.startswith("linux") and os.environ.get("CI")),
    reason="Docker is only *expected* on the Linux CI legs",
)
def test_docker_is_available_where_it_is_expected():
    """The skip above is load-bearing: without Docker every boundary test in
    this file disappears and the run still reports success.

    Gated on Linux CI rather than asserted everywhere. Docker is genuinely
    optional on a developer machine and the Windows runners default to Windows
    containers, so demanding it there would fail for a reason that is not a
    defect. On the Ubuntu legs it is expected, and its absence should be a
    failure rather than a quietly smaller test count.
    """
    assert _docker_works(), (
        "docker is unavailable on a Linux CI leg, so every sandbox boundary "
        "test was skipped and the container is unverified"
    )
