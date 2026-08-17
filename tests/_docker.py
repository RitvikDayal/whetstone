"""Docker helpers shared by the unit and integration suites.

A REAL MODULE, not a conftest. `tests/conftest.py` and `tests/unit/conftest.py`
both need these, and `from conftest import ...` resolves to whichever conftest
is nearest the importing test -- so a definition in one of them is invisible to
the other. That is not hypothetical: `test_writer_e2e.py` under
`tests/integration/` could not see the `sandbox_image` fixture, and copying the
conftest was the first thing I tried. Two copies of a skip condition is exactly
what the unit conftest's own docstring says must not happen.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import uuid

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


@pytest.fixture
def sandbox_image(tmp_path_factory) -> str:
    """A built pytest-capable image, or a skip with a reason.

    Shared because `test_verify.py` needed the same thing and reached for a tag
    I had built by hand on one machine -- which passed locally and failed on
    both Linux CI legs, since the image simply was not there. `availability()`
    checks the daemon, not the image, so nothing skipped: the tests ran and the
    container could not start.

    A FAILED BUILD IS NOT A SKIP where the build is expected to work. That is
    `test_reproduce.py`'s argument and it applies identically here: a registry
    outage or a rate limit would otherwise skip the only tests that prove the
    chain, and the Linux leg would still report success.
    """
    if not docker_works():
        pytest.skip("no linux-container docker daemon is reachable")
    context = tmp_path_factory.mktemp("img")
    (context / "Dockerfile").write_text(
        "FROM python:3.11-slim\nRUN pip install --no-cache-dir pytest\n",
        encoding="utf-8",
    )
    # A unique tag: a shared runner can have two of these in flight, and a
    # fixed one makes them clobber each other's image.
    tag = f"whetstone-test-sandbox:{uuid.uuid4().hex[:12]}"
    built = subprocess.run(
        ["docker", "build", "-q", "-t", tag, str(context)],
        capture_output=True,
        timeout=900,
    )
    if built.returncode != 0:
        detail = built.stderr.decode("utf-8", "replace")[-300:]
        if build_is_expected():
            pytest.fail(f"the sandbox image failed to build on a Linux CI leg: {detail}")
        pytest.skip(f"could not build the test image: {detail!r}")
    return tag


def build_is_expected() -> bool:
    """True where building a container image should work, so a failure there is
    a defect rather than a reason to skip."""
    return bool(sys.platform.startswith("linux") and os.environ.get("CI"))
