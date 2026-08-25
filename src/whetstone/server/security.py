"""What stands between any page in the user's browser and this project.

READ THIS BEFORE RELAXING ANYTHING HERE. There are exactly TWO controls, not
four, and they do not overlap:

| Attack | The only thing that stops it |
|---|---|
| Any web page issuing writes to the API (CSRF) | **The token** |
| An attacker's domain re-resolving to 127.0.0.1 (DNS rebinding) | **The Host check** |

The two properties that look like controls and are not:

- **Binding 127.0.0.1** stops nothing browser-mediated. The user's own browser
  is on the loopback interface.
- **Emitting no CORS headers** stops an attacker READING a response. It does
  not stop the request arriving and taking effect. A cross-origin `GET`, or a
  `POST` with a CORS-safelisted `Content-Type`, is a *simple request*: no
  preflight is sent, the handler runs, and only the response is withheld from
  the attacker's JavaScript. For anything that changes state, "they cannot read
  the answer" is not a defence.

So the token is a single point of failure for every write, and the Host check
is a single point of failure for rebinding. Neither substitutes for the other,
and there is no third thing to fall back on. Do not exempt a route from the
token because "there are other controls" -- there are not.

WHY THIS IS PLAIN HTTP, WHICH IS A DECISION AND NOT AN OVERSIGHT. The token
travels in a header over `http://127.0.0.1`, unencrypted. Reading that traffic
requires either code execution on this machine -- at which point the attacker
can read the token out of this process's memory and the transport is irrelevant
-- or, on Windows, an administrator-installed capture driver, which is the same
bar. The two attacks this file actually defends against are both browser
mediated and neither is helped by TLS.

The alternatives are worse, concretely. A self-signed certificate puts a
browser interstitial in front of the tool every time it starts, which teaches
the user to click through certificate warnings -- a habit with a far larger
blast radius than the risk it buys off. A locally-trusted CA means installing a
root certificate on the user's machine, which is permanent, machine-wide, and a
genuinely larger exposure than an unencrypted loopback socket. Browsers treat
`http://127.0.0.1` as a SECURE CONTEXT for exactly this reason: the platform has
already made this trade.

WHAT IS DELIBERATELY NOT BEHIND THE TOKEN. The static bundle. It has to be,
and the reasoning is not a compromise: the token reaches the app through the
URL fragment, fragments are never sent to a server, so the very first request
for `index.html` cannot carry one. A token-gated shell 401s on the request that
would have loaded the JavaScript that reads the token -- a deadlock on the
first request of the happy path. The bundle is public AGPL JavaScript with no
user data in it (`test_server_security.py` asserts that), so gating it protects
nothing and costs the product its boot. The Host check still applies to it.
"""

from __future__ import annotations

import hmac
import time

import anyio
from starlette.datastructures import MutableHeaders
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse
from starlette.types import ASGIApp, Message, Receive, Scope, Send

TOKEN_HEADER = "x-whetstone-token"

# Applied to every response. The API's is stricter still -- see below.
_ALWAYS = {
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
}

# The shell only. `default-src 'none'` with an explicit allowance per kind, so
# a directive nobody thought about falls back to "no".
#
# WHY THIS MATTERS MORE HERE THAN ON AN ORDINARY APP: the strings this UI
# renders -- finding titles, details, grade reasons, skip text -- are model
# output derived from the contents of a repository the user did not
# necessarily write. `doctor.py` states the same fact about `whetstone init`:
# whoever wrote the repository is not necessarily the person running it. Script
# execution in this page holds the token, and the token reaches every route.
#
# `'unsafe-inline'` is absent and must stay absent; the Vite build is
# configured with `modulePreload.polyfill = false` so it emits no inline
# script for this policy to have to permit.
_CSP = (
    "default-src 'none'; "
    "script-src 'self'; "
    "style-src 'self'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'; "
    "form-action 'none'"
)


def allowed_hosts(port: int) -> frozenset[str]:
    """The exact `Host` values this server answers to, port included.

    THE PORT IS PART OF THE MATCH, which is why this is not
    `TrustedHostMiddleware`. Starlette's implementation splits the header on
    ":" and compares the hostname alone, so an entry of "127.0.0.1:7727"
    matches nothing and an entry of "127.0.0.1" silently drops the port half of
    the check -- a control that reads as configured and is not.
    """
    return frozenset({f"127.0.0.1:{port}", f"localhost:{port}"})


# How much of an unauthenticated request body to read before answering. Bounded
# on purpose: draining without a limit lets an unauthenticated caller hold a
# worker open by streaming forever, which trades a cosmetic reset for a denial
# of service. Past the cap the response goes out anyway and the client may see
# a reset -- the right trade, because at that point the caller is not a browser
# sending a decision.
_DRAIN_LIMIT_BYTES = 1 << 20
_DRAIN_LIMIT_MESSAGES = 64
# AND A CLOCK. Bytes and messages bound how MUCH is read; neither bounds how
# LONG it takes. A client sending one byte a minute satisfies both limits and
# holds a worker for an hour -- the slow-loris shape, reachable here without a
# token because draining happens before the request is authenticated. The whole
# point of this function is a courtesy to a well-behaved client; two seconds is
# far more than loopback needs and far less than an attack wants.
_DRAIN_LIMIT_SECONDS = 2.0


async def _drain(receive: Receive) -> None:
    """Consume the request body so the client can finish writing it.

    Bounded three ways, and past any of them the response goes out anyway. The
    client may then see a connection reset -- which is the right trade, because
    a caller that has exceeded these is not a browser sending a decision.
    """
    deadline = time.monotonic() + _DRAIN_LIMIT_SECONDS
    read = 0
    for _ in range(_DRAIN_LIMIT_MESSAGES):
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return
        # `move_on_after`, not a bare await: `receive()` blocks until the client
        # sends something, and a client that sends NOTHING is the cheapest
        # version of this attack -- one connection, no traffic, one worker held
        # indefinitely. `message` stays None when the scope times out, which is
        # how the timeout is distinguished from a real message.
        message: Message | None = None
        with anyio.move_on_after(remaining):
            message = await receive()
        if message is None:
            return
        if message["type"] != "http.request":
            return
        read += len(message.get("body", b""))
        if not message.get("more_body", False) or read >= _DRAIN_LIMIT_BYTES:
            return


class HostGuard:
    """Reject any request whose `Host` is not exactly one of ours.

    THIS IS THE ANTI-REBINDING CONTROL. An attacker's page at `evil.com` whose
    DNS re-resolves to 127.0.0.1 is same-origin with this server as far as the
    browser is concerned, so the token can be read and sent by their script --
    but the browser still sends `Host: evil.com`, which it cannot forge. That
    header is the one thing about a rebound request that stays honest.

    403 AND NO REDIRECT. `TrustedHostMiddleware` answers a disallowed host with
    a 307 by default, and a 307 preserves method and body -- so a cross-origin
    POST that the check "blocked" is re-issued by the browser against the
    allowed host and arrives with an allowed `Host`. The control turns itself
    into a bypass. Nothing here ever emits a Location header.

    A MISSING OR REPEATED `Host` IS A REJECTION, not a default. HTTP/1.0 has no
    Host, and two Host headers are a request-smuggling shape; neither is
    something a browser on loopback produces.
    """

    def __init__(self, app: ASGIApp, port: int) -> None:
        self.app = app
        self.allowed = allowed_hosts(port)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        found = [
            value.decode("latin-1")
            for key, value in scope["headers"]
            if key == b"host"
        ]
        if len(found) != 1 or found[0].lower() not in self.allowed:
            # Drained for the same reason the token guard drains -- see there.
            # A refused POST whose body was never read reaches the client as a
            # connection reset rather than as the 403 that was sent.
            if scope["type"] == "http":
                await _drain(receive)
            response = PlainTextResponse(
                "This server answers only to 127.0.0.1 and localhost on its own "
                "port. A request arriving under any other name is either "
                "misrouted or a DNS-rebinding attempt.",
                status_code=403,
                headers=dict(_ALWAYS),
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


class TokenGuard:
    """Require the session token on every `/api/` route, including GET.

    INCLUDING GET, and that is not caution for its own sake. A `GET` that
    returns findings returns the contents of the user's repository as read by a
    model; and once any route is exempt, the exemption is what a future change
    widens. There is nothing else standing behind this.

    `hmac.compare_digest` rather than `==`: a byte-at-a-time comparison leaks
    the prefix through timing, and the attacker here can issue requests as fast
    as loopback allows.
    """

    def __init__(self, app: ASGIApp, token: str, *, prefix: str = "/api/") -> None:
        self.app = app
        self.token = token
        self.prefix = prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not scope["path"].startswith(self.prefix):
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        presented = request.headers.get(TOKEN_HEADER, "")
        if not hmac.compare_digest(presented, self.token):
            # DRAIN FIRST. Answering a POST without consuming its body leaves
            # the client still writing into a socket the server is closing, and
            # the client sees ConnectionResetError instead of the 401 that was
            # actually sent. Measured on Windows against this exact route: the
            # SPA would have rendered "network error" for a plain expired
            # token, which is the least actionable message available.
            await _drain(receive)
            response = JSONResponse(
                {
                    "error": (
                        "This request carried no valid session token. The token "
                        "is printed by `whetstone ui` and is different every "
                        "time it starts."
                    )
                },
                status_code=401,
                headers=dict(_ALWAYS),
            )
            await response(scope, receive, send)
            return

        await self.app(scope, receive, send)


class ResponseHeaders:
    """Stamp the security headers, and never a CORS one.

    A MIDDLEWARE RATHER THAN A HELPER EACH ROUTE CALLS, because the routes that
    matter are the ones nobody remembered: 404s, validation errors, the
    exception handler, and the streaming response. A helper covers the routes
    somebody thought about.

    `no-store` on the API is not politeness. Findings are the contents of a
    private repository as read by a model; cached to disk they outlive the
    server, the token and the session.
    """

    def __init__(self, app: ASGIApp, *, api_prefix: str = "/api/") -> None:
        self.app = app
        self.api_prefix = api_prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        is_api = scope["path"].startswith(self.api_prefix)

        async def _send(message: Message) -> None:
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                for name, value in _ALWAYS.items():
                    headers[name] = value
                if is_api:
                    headers["cache-control"] = "no-store"
                else:
                    headers["content-security-policy"] = _CSP
            await send(message)

        await self.app(scope, receive, _send)
