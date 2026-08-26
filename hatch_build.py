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
import pathlib
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

        # EVERY NO-BUILD RETURN SAYS SO. Three of these returned silently, and
        # a build that skipped the front-end then looked exactly like one that
        # included it -- right up until `whetstone ui` refused to start on a
        # machine where nobody could see this output any more. A skip with no
        # reason is the defect this whole project is about.
        if os.environ.get(SKIP):
            self._say(f"{SKIP} is set; not building the front-end.")
            return

        # BEFORE the `INDEX.is_file()` check below, not after. `is_file()`
        # FOLLOWS a symlink, so a symlinked `dist` would satisfy it and return
        # early -- past the boundary check entirely. And `dist` needs resolving
        # in its own right: `vite.config.ts` sets `emptyOutDir: true`, so a
        # build does not merely write there, it DELETES the directory first.
        # A symlinked `dist` therefore aims a recursive delete at whatever the
        # link points to.
        self._guard_write_boundary()

        if INDEX.is_file():
            # Already built. See STALENESS above -- this is the common case for
            # `uv sync`, and rebuilding here would front every test run with a
            # JavaScript build.
            self._say(f"front-end already built at {INDEX.parent}; reusing it.")
            return
        if not (UI / "package.json").is_file():
            self._say(
                f"no front-end source at {UI}; nothing to build. A wheel from "
                "this tree will have no control plane in it."
            )
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

    def _say(self, message: str) -> None:
        """A build-time note. Not a warning -- these are ordinary outcomes."""
        print(f"whetstone: {message}", file=sys.stderr)

    def _warn(self, message: str) -> None:
        print(f"warning: {message}", file=sys.stderr)

    def _guard_write_boundary(self) -> None:
        """Refuse to run npm anywhere but inside this repository.

        `npm ci` and `npm run build` write `node_modules/` and `dist/` into
        their working directory, and that directory is built from `__file__`
        without resolving it. A symlinked `src/whetstone/ui` therefore points
        the build at whatever the link targets -- outside the worktree, with no
        boundary check anywhere in the path.

        This project's rule is that writes stay inside the resolved worktree
        and the barrier is enforced in code rather than requested in prose. A
        build hook is not exempt from it just because it runs before anything
        else does.
        """
        root = pathlib.Path(__file__).resolve().parent
        # `dist` AS WELL AS `ui`. Resolving only the parent leaves the output
        # directory free to be a link of its own, and `emptyOutDir: true` makes
        # that a recursive delete rather than a stray write.
        for label, path in (("front-end directory", UI), ("build output", UI / "dist")):
            if not path.exists() and label == "build output":
                continue
            target = path.resolve()
            if not target.is_relative_to(root):
                raise RuntimeError(
                    f"the {label} resolves to {target}, which is outside this "
                    f"repository ({root}). `npm ci` writes node_modules/ and "
                    "`npm run build` EMPTIES and rewrites dist/, so building "
                    "there would write -- and delete -- outside the worktree. "
                    "Refusing. This is what a symlink here looks like."
                )
            if ".git" in target.parts:
                raise RuntimeError(
                    f"the {label} resolves to {target}, which is inside .git. "
                    "Refusing: a build there rewrites repository internals."
                )
