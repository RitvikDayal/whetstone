"""The built control-plane bundle: one guard, and it FAILS rather than skips.

WHY THIS IS SHARED AND WHY IT IS LOUD. Three test modules decide whether to run
based on whether `src/whetstone/ui/dist/index.html` exists. If it does not,
every one of them skips -- and what they cover is the entire control plane: the
security envelope over a real socket, the CSP not blocking its own bundle, and
the rendered DOM agreeing with the API. A green suite that verified none of that
is exactly the "check that quietly does not run" defect this project keeps
paying for, and it would be reached by forgetting one `npm run build`.

So this is `tests/_browser.py` again, for a different missing artifact and for
the same reason. On CI the bundle is not optional and its absence raises at
import; anywhere else a developer who has not run the front-end build can still
run the Python suite.

`tests/unit/test_package.py` is the other half: it BUILDS a wheel and asserts
the bundle is inside it, with no skip branch at all. That is what guarantees a
release cannot ship without one. This guarantees a CI run cannot go green
without exercising it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

DIST = Path(__file__).resolve().parents[1] / "src" / "whetstone" / "ui" / "dist"
INDEX = DIST / "index.html"

_BUILD = "npm --prefix src/whetstone/ui ci && npm --prefix src/whetstone/ui run build"


def bundle_is_expected() -> bool:
    """CI must have a built bundle. Anywhere else it is optional.

    Keyed on `CI` alone, exactly as `tests/_browser.py` keys the browser: a
    missing artifact there is a broken pipeline rather than an environment
    nobody set up.
    """
    return bool(os.environ.get("CI"))


BUNDLE_MISSING = None if INDEX.is_file() else f"the control-plane bundle is not built at {DIST}"

if BUNDLE_MISSING and bundle_is_expected():  # pragma: no cover - CI guard
    raise RuntimeError(
        f"{BUNDLE_MISSING}. Every control-plane test would otherwise SKIP and "
        f"the leg would still be green -- covering nothing of the security "
        f"envelope, the CSP, or the rendered DOM. The workflow runs "
        f"`{_BUILD}` before `uv sync`; if that step was removed or reordered, "
        f"this is what it looks like."
    )

needs_bundle = pytest.mark.skipif(
    BUNDLE_MISSING is not None, reason=BUNDLE_MISSING or "bundle present"
)
