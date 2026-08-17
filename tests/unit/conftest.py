"""Unit-suite fixtures. Docker helpers live in `tests/_docker.py`.

Re-exported here so `from conftest import needs_docker` keeps working in the
unit modules that already do it, while there remains exactly one definition.
"""

from __future__ import annotations

from _docker import (
    build_is_expected as build_is_expected,
)
from _docker import (
    docker_expected as docker_expected,
)
from _docker import (
    docker_works as docker_works,
)
from _docker import (
    needs_docker as needs_docker,
)
from _docker import (
    sandbox_image as sandbox_image,
)
