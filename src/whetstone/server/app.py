"""The control plane's HTTP surface.

READ `security.py` FIRST. There are two controls and neither substitutes for
the other; this module wires them and adds nothing to them.

WHAT THIS SERVER IS NOT. It is not multi-user, not remote, and not a service.
It is one person's local window onto one project's store, started by a command
in their own terminal and dying with it. Everything here is sized for that:
one config loaded at startup, a token minted per process, and a connection
opened per request rather than pooled.

A CONNECTION PER REQUEST, deliberately. `sqlite3` connections are not safe to
share across threads and uvicorn serves handlers on a thread pool, so a
module-level connection is a data race waiting for a second tab. Opening one
costs microseconds against a file that is already in the page cache.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Any

from ..config.model import WhetstoneConfig
from ..errors import WhetstoneError
from ..readmodel import cost_view, findings_view, run_view, trust_view
from ..store.db import connect
from .assets import assets_root
from .security import HostGuard, ResponseHeaders, TokenGuard


def missing_ui_extra() -> WhetstoneError:
    """The error for "the web dependencies are not installed".

    ONE MESSAGE, shared by every import site. `serve.py` imports uvicorn and
    this module imports FastAPI, and the first version let uvicorn's ImportError
    escape uncaught -- so whether the user got an explanation or a traceback
    depended on which of the two happened to be missing.

    The same split `browser.py` draws: a MISSING PACKAGE is not a missing
    binary and not a missing build. `ModuleNotFoundError: fastapi` is a true
    statement that tells a user nothing about what they are supposed to type.
    """
    return WhetstoneError(
        "the control plane needs FastAPI and uvicorn, which are an optional "
        "extra rather than a base dependency -- installing Whetstone to run "
        "`hygiene` should not also install a web server. Install them with "
        "`pip install 'whetstone-cli[ui]'`."
    )


def _require_web_dependencies():
    try:
        from fastapi import FastAPI
        from fastapi.responses import JSONResponse
        from starlette.staticfiles import StaticFiles
    except ImportError as exc:
        raise missing_ui_extra() from exc
    return FastAPI, JSONResponse, StaticFiles


def create_app(
    *,
    config: WhetstoneConfig,
    project_root: Path,
    state_root: Path,
    token: str,
    port: int,
):
    """The ASGI application, already wrapped in its security envelope.

    The envelope is applied HERE rather than left to the caller. A factory that
    returns a bare app and trusts every caller to wrap it is a factory whose
    second caller ships an unguarded server -- and the test suite would be the
    first such caller.
    """
    FastAPI, JSONResponse, StaticFiles = _require_web_dependencies()

    # Assets are resolved BEFORE the server binds, so a missing bundle is a
    # sentence in the terminal the user is already looking at rather than a
    # blank page they have to open devtools to explain.
    dist = assets_root()

    app = FastAPI(
        title="Whetstone control plane",
        # No interactive docs: they are a second, unaudited surface over the
        # same routes, and this API has exactly one consumer that ships with it.
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @contextlib.contextmanager
    def _store():
        with contextlib.closing(connect(state_root)) as conn:
            yield conn

    @app.exception_handler(WhetstoneError)
    async def _whetstone_error(_request, exc: WhetstoneError):
        # WhetstoneError subclasses already carry a sentence written for a
        # human -- `dispositions.py` in particular names the missing argument
        # AND why it is required. Returned as-is rather than replaced with a
        # second, worse message.
        return JSONResponse({"error": str(exc)}, status_code=400)

    @app.get("/api/findings")
    def findings() -> dict[str, Any]:
        """The queue, plus the run it came from.

        `state="queued"` MATCHES THE CLI'S DEFAULT, and that is a choice rather
        than an accident: `whetstone findings` defaults `--state` to `queued`
        while `findings_view` defaults to None, which means do not filter.
        Those are different queries, and a surface that called the read model
        with no arguments and described the result as "the same list the CLI
        shows" would be showing a different list.
        """
        with _store() as conn:
            return {
                "findings": findings_view(conn, state="queued"),
                "run": run_view(conn),
            }

    @app.get("/api/trust")
    def trust() -> list[dict[str, Any]]:
        with _store() as conn:
            return trust_view(conn, config)

    @app.get("/api/costs")
    def costs() -> dict[str, Any]:
        """Every recorded cost record. TAKES NO RUN ID, deliberately.

        A per-run route would join a caller-supplied id onto a filesystem path,
        and run ids reach this process from the store rather than from a
        constant. That is a path-traversal shape, and the cheapest way not to
        have one is not to build the route: the whole set is a few kilobytes
        and the screen renders all of it anyway.
        """
        return cost_view(state_root)

    @app.get("/api/config")
    def resolved_config() -> dict[str, Any]:
        """What this run would be held to, read off the loaded config.

        The ceiling is reported WITH the fact that it is per lens rather than
        per run (issue #43) and that `calls_per_day` is accepted and not
        enforced. A number rendered without those two sentences is a bound the
        user believes they have.
        """
        ceiling = config.budget.ceiling
        return {
            "project": config.project.name,
            "tier": str(config.budget.tier),
            "usd_per_run": ceiling.usd_per_run,
            "calls_per_day": ceiling.calls_per_day,
            "lenses": sorted(config.lenses),
            "caveats": [
                caveat
                for caveat in (
                    (
                        "`usd_per_run` is enforced PER LENS, not per run, so two "
                        "enabled lenses can together spend twice it. Issue #43."
                    )
                    if ceiling.usd_per_run is not None
                    else None,
                    (
                        "`calls_per_day` is accepted and NOT enforced -- "
                        "Whetstone keeps no cross-run call accounting."
                    )
                    if ceiling.calls_per_day is not None
                    else None,
                    (
                        "Nothing bounds how many runs are started. Each run is "
                        "bounded; the total is not."
                    ),
                )
                if caveat is not None
            ],
            "project_root": str(project_root),
        }

    # The shell, LAST and unauthenticated. See `security.py`: the token arrives
    # in the URL fragment, which is never sent to a server, so a token-gated
    # `index.html` 401s the request that would have loaded the JavaScript that
    # reads the token. `html=True` serves index.html for `/`.
    app.mount("/", StaticFiles(directory=dist, html=True), name="ui")

    # Outermost first: response headers must reach the Host guard's own 403 and
    # every error the routes raise, so it wraps everything.
    return ResponseHeaders(HostGuard(TokenGuard(app, token), port))
