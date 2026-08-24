"""Binding the socket, minting the token, and opening the browser.

THE SOCKET IS BOUND ONCE AND HANDED TO UVICORN, rather than probing for a free
port and then asking uvicorn to bind that number. The probe-then-bind spelling
has a window between the probe closing its socket and the server opening its
own, and on Windows that window is worse than a race: `SO_REUSEADDR` there
permits a SECOND process to bind a port another process is already listening
on and steal incoming connections -- the opposite of POSIX semantics -- and
uvicorn sets `SO_REUSEADDR`. A local process that won either race would serve
its own page to the browser about to be opened, and read the session token out
of the URL it was handed.

So: one socket, `SO_EXCLUSIVEADDRUSE` where that exists, the port read back off
the socket that is already listening, and the browser opened only afterwards.
"""

from __future__ import annotations

import contextlib
import secrets
import socket
import webbrowser
from pathlib import Path

from ..config.model import WhetstoneConfig

# 32 bytes from the OS CSPRNG, url-safe. SPECIFIED HERE rather than left to
# the implementer, because every plausible weak spelling passes every test that
# checks the token is *required*: `uuid4()` is not guaranteed to come from a
# CSPRNG, and `random.random()` is a seeded Mersenne Twister. This token
# reaches every route, so guessing it is not an information leak.
_TOKEN_BYTES = 32

# Loopback, and there is no setting that changes it. A control plane reachable
# from the network is a different product with a different threat model.
BIND_HOST = "127.0.0.1"


def mint_token() -> str:
    return secrets.token_urlsafe(_TOKEN_BYTES)


def bind(port: int = 0) -> socket.socket:
    """A listening socket on loopback. Port 0 means let the OS choose."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    exclusive = getattr(socket, "SO_EXCLUSIVEADDRUSE", None)
    if exclusive is not None:  # Windows
        # Refuses a second bind to the same port outright, which is what
        # SO_REUSEADDR fails to do there. Set BEFORE bind or it has no effect.
        sock.setsockopt(socket.SOL_SOCKET, exclusive, 1)
    try:
        sock.bind((BIND_HOST, port))
        sock.listen(128)
    except OSError:
        sock.close()
        raise
    return sock


def url_for(port: int, token: str) -> str:
    """The address to open. The token is in the FRAGMENT -- see `session.ts`."""
    return f"http://{BIND_HOST}:{port}/#t={token}"


def serve(
    *,
    config: WhetstoneConfig,
    project_root: Path,
    state_root: Path,
    port: int = 0,
    open_browser: bool = True,
    announce=print,
    show_url: bool = False,
) -> None:
    """Run the control plane until interrupted.

    `announce` is injected so tests can read what a user would see without
    capturing stdout, and so the CLI can print through Rich.

    THE PRINTED URL CARRIES NO TOKEN unless `show_url` is set. A terminal is
    not a private surface: scrollback is screen-shared, piped through `tee`,
    captured by `Start-Transcript`, and pasted into chat windows. The full URL
    goes to `webbrowser.open` and nowhere else. `--print-url` exists for people
    running with `--no-open`, and it says out loud what the line grants.
    """
    # Imported late for the same reason `create_app` imports FastAPI late: a
    # user without the `ui` extra should get a sentence naming the extra, not
    # a ModuleNotFoundError from an import at the top of a module the CLI
    # touches on every invocation.
    #
    # Through the SAME error as FastAPI's. The first version let uvicorn's
    # ImportError escape raw, so whether the user got an explanation or a
    # traceback depended on which of the two packages was missing.
    from .app import create_app, missing_ui_extra

    try:
        import uvicorn
    except ImportError as exc:
        raise missing_ui_extra() from exc

    token = mint_token()
    sock = bind(port)
    try:
        bound_port = sock.getsockname()[1]
        app = create_app(
            config=config,
            project_root=project_root,
            state_root=state_root,
            token=token,
            port=bound_port,
        )
        full_url = url_for(bound_port, token)

        announce(f"Whetstone control plane on http://{BIND_HOST}:{bound_port}/")
        if show_url:
            # TWO CALLS, not one string with a newline in it. The CLI passes
            # every announced line through `_printable`, which renders control
            # characters visible so a model-authored or repo-read string cannot
            # drive the terminal -- and it does not make an exception for a
            # newline this module put there itself. Measured: the URL printed
            # with a trailing literal `\\x0a`, which is both ugly and a
            # character a user copying the line would take with them.
            announce(f"  {full_url}")
            announce(
                "  Anyone who reads that line can act on this project. It is "
                "printed because --print-url was given."
            )
        elif not open_browser:
            announce(
                "  The session token is not printed. Re-run with --print-url "
                "to get a pasteable link, or drop --no-open to have the "
                "browser opened for you."
            )
        announce("  Press Ctrl+C to stop.")

        if open_browser:
            # AFTER the socket is listening. `bind()` has already called
            # listen(), so a connection arriving now is queued by the kernel
            # rather than refused -- the first load cannot lose the race.
            webbrowser.open(full_url)

        server = uvicorn.Server(
            uvicorn.Config(
                app,
                # Off. The access log prints the request path, and while the
                # token never travels in a path today, a future route that
                # took one would put it in the user's scrollback silently.
                access_log=False,
                log_level="warning",
                # The socket is already bound; host/port here are unused.
                lifespan="off",
            )
        )
        server.run(sockets=[sock])
    finally:
        # uvicorn closes the socket it was handed on a clean shutdown, so this
        # is usually a no-op -- but NOT closing it on the paths where uvicorn
        # never ran (a create_app failure, a KeyboardInterrupt during the
        # announce) leaks a listening port for the life of the process.
        with contextlib.suppress(OSError):
            sock.close()
