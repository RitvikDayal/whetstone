"""Build the control plane's front-end when the Python package is built.

WHAT THIS IS AND IS NOT RESPONSIBLE FOR. This hook is a CONVENIENCE. The thing
that actually stops a wheel shipping without its UI is
`tests/unit/test_package.py`, which opens the built wheel's zip and fails if
the assets are not inside it. That distinction was measured rather than
assumed:

- `pip install whetstone-cli` from a wheel never runs this hook at all -- a
  wheel is a zip and the build backend is not involved. So a hook that raises
  protects nobody on the main install path.
- Installing from an sdist DOES run it. Raising there turns "no Node on this
  machine" into a failed install for someone who did nothing wrong, which is
  why the sdist carries `dist/` already (see `[tool.hatch.build] artifacts`)
  and why the no-Node case below WARNS instead of raising.
- `uv sync` runs it too, because an editable install invokes the backend. A
  hook that shelled out to npm on every sync would put a JavaScript build in
  front of every `uv run pytest`.

So: build when there is something to build and a toolchain to build it with,
say something useful when there is not, and let the wheel test be the gate.

STALENESS IS NOT DETECTED, deliberately. Git does not preserve mtimes, so
comparing timestamps across a fresh checkout is guesswork that would either
rebuild constantly or skip a rebuild that was needed. The contract is explicit
instead: an existing `dist/` is taken as intended, and anyone changing the
front-end runs `npm run build`. CI builds it from scratch on every run, so what
ships is never a stale local artifact.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

from hatchling.builders.hooks.plugin.interface import BuildHookInterface

UI = Path(__file__).parent / "src" / "whetstone" / "ui"
INDEX = UI / "dist" / "index.html"

# Set by CI, which builds the front-end as an explicit step before anything
# touches Python. Also the escape hatch for anyone who wants a Python-only
# build and knows the wheel will fail its own packaging test.
SKIP = "WHETSTONE_SKIP_UI_BUILD"


class CustomBuildHook(BuildHookInterface):
    PLUGIN_NAME = "custom"

    def initialize(self, version: str, build_data: dict) -> None:
        del version, build_data

        if os.environ.get(SKIP):
            return
        if INDEX.is_file():
            # Already built. See STALENESS above -- this is the common case for
            # `uv sync`, and rebuilding here would front every test run with a
            # JavaScript build.
            return
        if not (UI / "package.json").is_file():
            # An sdist that legitimately carries no front-end source. Nothing
            # to build and nothing to say.
            return

        npm = shutil.which("npm")
        if npm is None:
            # NOT an error. See the module docstring: this path is reached by a
            # legitimate sdist install on a machine with no Node, and failing
            # there breaks an install that would otherwise work.
            self._warn(
                "the control plane's front-end is not built and npm is not on "
                "PATH, so this build cannot build it. The Python package will "
                "install and every command except `whetstone ui` will work; "
                "`whetstone ui` will refuse with an explanation. To build it, "
                "install Node and run `npm --prefix src/whetstone/ui ci && "
                "npm --prefix src/whetstone/ui run build`."
            )
            return

        for command in (["ci"], ["run", "build"]):
            result = subprocess.run(
                [npm, *command],
                cwd=UI,
                capture_output=True,
                text=True,
                check=False,
            )
            if result.returncode != 0:
                # This one DOES raise. Node is present and the build failed,
                # which is a broken front-end rather than a missing toolchain --
                # shipping a wheel with a half-built UI is the thing all of this
                # exists to prevent.
                tail = (result.stderr or result.stdout or "").strip().splitlines()
                raise RuntimeError(
                    f"`npm {' '.join(command)}` failed in {UI} with exit "
                    f"{result.returncode}:\n" + "\n".join(tail[-20:])
                )

    def _warn(self, message: str) -> None:
        print(f"warning: {message}", file=sys.stderr)
