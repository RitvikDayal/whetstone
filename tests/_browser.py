"""The browser guard and the local server both browser test modules need.

WHY THIS IS SHARED RATHER THAN COPIED. `test_rendered_ui.py` stated in its own
docstring that its browser tests "fail rather than skip on the Linux CI legs"
and implemented nothing of the kind -- its `_needs_browser` skipped everywhere.
The guarantee held only because `test_browser.py` raised at import time and
failed collection for the whole session. Move, rename or delete that guard and
six tests would have started skipping silently on a green leg with the
docstring still claiming otherwise. The claim and the code that enforces it now
live in the same file, and both modules import it rather than restating it.

EVERY CI LEG, not only Linux. The workflow installed Chromium on Linux alone,
so the two Windows legs passed with no rendered-ui coverage at all -- the same
"check that quietly does not run" defect one level up. The workflow now
installs it everywhere and this guard expects it everywhere, so a removed
install step is loud on all four legs instead of invisible on two.
"""

from __future__ import annotations

import http.server
import os
import threading
from pathlib import Path

import pytest

from whetstone.lenses.rendered_ui.browser import availability


def browser_is_expected() -> bool:
    """CI must have a browser. Anywhere else it is optional.

    Keyed on `CI` alone. A missing binary there is a broken pipeline rather
    than an environment nobody set up, and a developer without Chromium
    installed should still be able to run the rest of the suite.
    """
    return bool(os.environ.get("CI"))


UNAVAILABLE = availability()

if UNAVAILABLE and browser_is_expected():  # pragma: no cover - CI guard
    raise RuntimeError(
        f"a browser is required on every CI leg and is not available: "
        f"{UNAVAILABLE}. The workflow runs `playwright install chromium`; if "
        "that step was removed these tests would silently skip and the leg "
        "would still be green."
    )

needs_browser = pytest.mark.skipif(
    UNAVAILABLE is not None, reason=UNAVAILABLE or "browser available"
)


class Server:
    """A local HTTP server, because `file://` has no origin worth pinning.

    THREADED, and that is the point of sharing it. The other copy of this
    fixture used a plain `HTTPServer`, which serves one request at a time.
    Chromium issues a document request and a favicon request per render, and
    `capture()` renders every check twice per viewport, so serialised serving
    pushed the wait for `networkidle` toward the 10-second cap `_settle()`
    imposes and then swallows. A test that is slow for a reason nobody can see
    is one that eventually fails for a reason nobody can see.

    `redirect_to` serves a real 302 from `/away`, which is what makes the
    redirect test deterministic -- a `<meta refresh>` races the load state.
    """

    def __init__(self, root: Path, redirect_to: str | None = None) -> None:
        self.redirect_to = redirect_to
        outer = self

        def _do_get(handler_self):
            if outer.redirect_to and handler_self.path.rstrip("/").endswith("away"):
                handler_self.send_response(302)
                handler_self.send_header("Location", outer.redirect_to)
                handler_self.end_headers()
                return
            http.server.SimpleHTTPRequestHandler.do_GET(handler_self)

        handler = type(
            "H",
            (http.server.SimpleHTTPRequestHandler,),
            {
                "directory": str(root),
                "log_message": lambda *a, **k: None,
                "do_GET": _do_get,
                "__init__": lambda s, *a, **k: http.server.SimpleHTTPRequestHandler.__init__(  # noqa: E501
                    s, *a, directory=str(root), **k
                ),
            },
        )
        self._httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)

    def __enter__(self) -> str:
        self._thread.start()
        return f"http://127.0.0.1:{self.port}"

    def __exit__(self, *_exc: object) -> None:
        # `shutdown()` BLOCKS until `serve_forever` acknowledges, so calling it
        # on a server that was never entered waits on a thread that never
        # started -- a hung CI leg instead of a failing one. Nothing reaches
        # that today; a `with` chain that raises before this one is entered
        # would.
        if self._thread.is_alive():
            self._httpd.shutdown()
        self._httpd.server_close()
