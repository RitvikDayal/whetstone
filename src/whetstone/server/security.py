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
