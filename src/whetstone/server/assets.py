"""The built single-page app, and what to say when it is not there.

WHY A MISSING BUNDLE IS AN ERROR WITH A SENTENCE RATHER THAN A 404. A 404 from
a web server reads as a routing bug, and the actual cause -- a wheel built
without `npm run build`, or a source checkout nobody has built -- is nowhere in
that response. The two are fixed in completely different places.

TWO SEPARATE FAILURES, REPORTED SEPARATELY, which is the distinction
`lenses/rendered_ui/browser.py` already draws between "the playwright package
is not installed" and "the browser binary is not downloaded". Telling someone
to `pip install whetstone-cli[ui]` when their real problem is an unbuilt `dist`
sends them to the wrong place, and they will do it, and it will not help.
"""

from __future__ import annotations

from pathlib import Path

from ..errors import WhetstoneError

# `src/whetstone/ui/dist`, which is where `vite.config.ts` puts it and what
# `pyproject.toml`'s `[tool.hatch.build] artifacts` copies into the wheel.
DIST = Path(__file__).resolve().parent.parent / "ui" / "dist"
INDEX = DIST / "index.html"


class AssetsMissingError(WhetstoneError):
    """The control plane's static bundle was not built or did not ship."""


def assets_root() -> Path:
    """The directory holding the built app, or raise saying how to get one."""
    if INDEX.is_file():
        return DIST

    # An installed wheel cannot be fixed by running npm in it, and a source
    # checkout cannot be fixed by reinstalling. The message says which
    # situation the reader is in rather than listing both remedies and letting
    # them guess.
    looks_installed = "site-packages" in str(DIST).replace("\\", "/")
    remedy = (
        "This looks like an installed copy, so the wheel was built without its "
        "front-end. Reinstall from a release wheel, or build from source with "
        "`npm --prefix src/whetstone/ui ci && npm --prefix src/whetstone/ui "
        "run build`."
        if looks_installed
        else "This looks like a source checkout. Build it with `npm --prefix "
        "src/whetstone/ui ci && npm --prefix src/whetstone/ui run build`."
    )
    raise AssetsMissingError(
        f"the control plane's built assets are not present at {DIST}. {remedy} "
        "The Python package is installed correctly -- this is the JavaScript "
        "bundle, which is a separate thing and fails separately."
    )
