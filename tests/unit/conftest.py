"""Shared fixtures and skip conditions for the unit suite.

`_docker_works` lived in two test modules, verbatim. Two copies gating
different test sets means a change to one skip condition silently leaves the
other behind -- and both of them guard the only tests that prove the sandbox
confines anything.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys

import pytest


def docker_works() -> bool:
    """True when a Linux-container daemon is actually reachable.

    The OSType check is not decoration: the Windows CI runners answer
    `docker info` and then fail `docker run` with "Windows does not support
    PidsLimit". A daemon that cannot run our containers is an unavailable
    sandbox, and a test gated on the weaker check fails instead of skipping.
    """
    if shutil.which("docker") is None:
        return False
    try:
        probe = subprocess.run(
            ["docker", "info", "--format", "{{.OSType}}"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return probe.returncode == 0 and probe.stdout.strip().lower() == "linux"


needs_docker = pytest.mark.skipif(
    not docker_works(), reason="no linux-container docker daemon is reachable"
)

# Where Docker is EXPECTED rather than optional. A developer laptop may not have
# it and the Windows runners default to Windows containers, so demanding it
# everywhere would fail for a reason that is not a defect.
docker_expected = pytest.mark.skipif(
    not (sys.platform.startswith("linux") and os.environ.get("CI")),
    reason="docker is only expected on the Linux CI legs",
)


def build_is_expected() -> bool:
    """True where building a container image should work, so a failure there is
    a defect rather than a reason to skip."""
    return bool(sys.platform.startswith("linux") and os.environ.get("CI"))
