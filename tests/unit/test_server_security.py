"""The security envelope, attacked rather than confirmed.

DRIVEN OVER A REAL SOCKET, not only through TestClient. TestClient synthesises
an ASGI scope, so it can present a `Host` the transport never had and can skip
header handling a real server does. The controls here are ABOUT headers, so at
least one test of each has to travel over TCP or it is measuring a fixture.

READ `src/whetstone/server/security.py` FIRST. There are two controls -- the
token for CSRF, the Host check for DNS rebinding -- and neither substitutes for
the other. Every test below is pinned to one of them.
"""

from __future__ import annotations

import ast
import contextlib
import json
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

# NOT `pytest.importorskip`. That skips the WHOLE MODULE when the extra
# is absent -- and on a CI leg that dropped `--all-extras`, every test in
# here would skip while the leg stayed green. `_bundle` raises instead
# wherever CI is set, and skips only on a developer machine.
from _bundle import UI_EXTRA_MISSING  # noqa: E402
from whetstone.config.loader import load_config
from whetstone.readmodel import ID_PREFIX
from whetstone.server import serve as serve_module
from whetstone.server.security import TOKEN_HEADER, allowed_hosts
from whetstone.store.db import connect

pytestmark = pytest.mark.skipif(
    UI_EXTRA_MISSING is not None, reason=UI_EXTRA_MISSING or "ui extra present"
)

# The shared guard, which FAILS rather than skips wherever CI is set.
# A local `skipif` let a forgotten `npm run build` turn these into skips
# on a green leg -- see `tests/_bundle.py`.
from _bundle import needs_bundle as needs_assets  # noqa: E402


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "whetstone.yaml").write_text(
        "version: 1\nproject:\n  name: guarded\nstate_dir: .state\n",
        encoding="utf-8",
    )
    with contextlib.closing(connect(tmp_path / ".state")) as conn:
        conn.execute(
            "INSERT INTO runs (id, tier, scope_mode, file_count, started_at, "
            "status, skipped_json) VALUES ('run-0000000001','quick','full',1,"
            "'2026-08-24T10:00:00+00:00','complete','[]')"
        )
    return tmp_path


@pytest.fixture
def live(project: Path):
    """A real uvicorn on a real loopback socket, in a thread.

    Returns (base_url, token, port). Torn down by asking the server to exit and
    joining the thread, so a failing test cannot leave a listener behind for
    the rest of the session.
    """
    import uvicorn

    from whetstone.server.app import create_app

    token = serve_module.mint_token()
    sock = serve_module.bind()
    port = sock.getsockname()[1]
    app = create_app(
        config=load_config(project / "whetstone.yaml"),
        project_root=project,
        state_root=project / ".state",
        token=token,
        port=port,
    )
    server = uvicorn.Server(
        uvicorn.Config(app, access_log=False, log_level="error", lifespan="off")
    )
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    _wait_until_listening(port, token)
    try:
        yield f"http://127.0.0.1:{port}", token, port
    finally:
        server.should_exit = True
        thread.join(timeout=30)


def _wait_until_listening(port: int, token: str) -> None:
    import time

    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            _request(f"http://127.0.0.1:{port}/api/findings", token=token)
            return
        except (urllib.error.URLError, ConnectionError):
            time.sleep(0.05)
    raise AssertionError("the test server never started listening")


def _request(url: str, *, token: str | None = None, host: str | None = None,
             origin: str | None = None, method: str = "GET"):
    """One request, returning (status, headers, body). 4xx is a RESULT here."""
    request = urllib.request.Request(url, method=method)
    if token is not None:
        request.add_header(TOKEN_HEADER, token)
    if host is not None:
        # The transport-level Host header, which is the thing the guard reads
        # and the thing a rebinding browser cannot forge.
        request.add_header("Host", host)
    if origin is not None:
        request.add_header("Origin", origin)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers), exc.read()


# --- the token ---------------------------------------------------------------


def test_an_api_request_with_no_token_is_refused(live):
    base, _token, _port = live
    status, _headers, body = _request(f"{base}/api/findings")
    assert status == 401
    assert b"token" in body.lower()


def test_an_api_request_with_the_wrong_token_is_refused(live):
    base, token, _port = live
    status, _h, _b = _request(f"{base}/api/findings", token=token[:-1] + "x")
    assert status == 401


def test_a_token_that_is_a_prefix_of_the_real_one_is_refused(live):
    """Guards the `startswith`-shaped mistake as well as the `==` one."""
    base, token, _port = live
    assert _request(f"{base}/api/findings", token=token[:20])[0] == 401


@pytest.mark.parametrize(
    "route", ["/api/findings", "/api/trust", "/api/costs", "/api/config"]
)
def test_every_api_route_requires_the_token_including_GET(live, route):
    """EVERY route, not a list somebody remembered to update.

    The routes are read off the app below so a new one cannot be added without
    appearing here; this parametrisation is the readable half.
    """
    base, token, _port = live
    assert _request(f"{base}{route}")[0] == 401
    assert _request(f"{base}{route}", token=token)[0] == 200


def test_no_api_route_escapes_the_guard(live):
    """The mechanical half: enumerate the app's own routes and try each one."""
    base, token, _port = live
    from whetstone.server.app import create_app  # noqa: F401  (import shape check)

    status, _h, body = _request(f"{base}/api/findings", token=token)
    assert status == 200
    # Every declared /api path must 401 unauthenticated. Read from the router
    # rather than restated, so adding a route to app.py without adding it here
    # still fails.
    import whetstone.server.app as app_module

    source = Path(app_module.__file__).read_text(encoding="utf-8")
    declared = {
        node.args[0].value
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in {"get", "post", "put", "delete", "patch"}
        and node.args
        and isinstance(node.args[0], ast.Constant)
        and isinstance(node.args[0].value, str)
        and node.args[0].value.startswith("/api/")
    }
    assert declared, "the route scan found nothing; it has stopped measuring"
    for route in sorted(declared):
        assert _request(f"{base}{route}")[0] == 401, route


# --- the Host check ----------------------------------------------------------


def test_a_foreign_host_is_refused_even_with_a_valid_token(live):
    """THE DNS-REBINDING TEST, and the token is present on purpose.

    A rebound page IS same-origin with this server as far as the browser is
    concerned, so its script can read and send the token. The only thing that
    stays honest is the Host header, which the browser sets and the page cannot
    forge. So this must fail on the Host check ALONE -- sending it without a
    token would pass for the wrong reason.
    """
    base, token, _port = live
    status, headers, _b = _request(f"{base}/api/findings", token=token, host="evil.com")
    assert status == 403
    assert "location" not in {k.lower() for k in headers}


def test_the_host_check_is_not_satisfied_by_a_redirect(live):
    """A 307 preserves method and body, so a "blocked" cross-origin POST would
    simply be re-issued against the allowed host and arrive allowed. Starlette's
    TrustedHostMiddleware does exactly that by default, which is why this
    server does not use it."""
    base, token, _port = live
    for host in ("evil.com", "127.0.0.1.evil.com", "localhost.evil.com"):
        status, headers, _b = _request(f"{base}/api/findings", token=token, host=host)
        assert status == 403, host
        assert not any(k.lower() == "location" for k in headers), host


def test_the_port_is_part_of_the_host_match(live):
    """`TrustedHostMiddleware` splits on ':' and compares the hostname alone,
    so configuring it with a port silently matches nothing and configuring it
    without one silently drops half the check. This server matches the whole
    value."""
    base, token, port = live
    assert _request(f"{base}/api/findings", token=token, host="127.0.0.1")[0] == 403
    assert _request(f"{base}/api/findings", token=token, host=f"127.0.0.1:{port + 1}")[0] == 403
    assert _request(f"{base}/api/findings", token=token, host=f"127.0.0.1:{port}")[0] == 200


def test_the_host_check_covers_the_static_shell_too(live):
    """The shell is exempt from the TOKEN, not from the Host check."""
    base, _token, _port = live
    assert _request(f"{base}/", host="evil.com")[0] == 403


def test_localhost_and_the_loopback_literal_are_both_accepted(live):
    base, token, port = live
    for host in (f"localhost:{port}", f"127.0.0.1:{port}", f"LOCALHOST:{port}"):
        assert _request(f"{base}/api/findings", token=token, host=host)[0] == 200, host


def test_the_allowlist_is_exactly_two_names(live):
    _base, _token, port = live
    assert allowed_hosts(port) == frozenset(
        {f"127.0.0.1:{port}", f"localhost:{port}"}
    )


# --- CORS, which protects reads and never writes -----------------------------


@pytest.mark.parametrize("route", ["/api/findings", "/"])
def test_no_cors_header_is_ever_emitted(live, route):
    base, token, _port = live
    _status, headers, _b = _request(
        f"{base}{route}", token=token, origin="https://evil.example"
    )
    lowered = {k.lower() for k in headers}
    assert "access-control-allow-origin" not in lowered
    assert "access-control-allow-credentials" not in lowered


def test_no_cors_header_on_a_rejection_either(live):
    """Error paths are where a header policy is usually forgotten."""
    for status_url, token in (("/api/findings", None), ("/api/findings", "wrong")):
        base, real, _port = live
        _s, headers, _b = _request(
            f"{base}{status_url}", token=token, origin="https://evil.example"
        )
        assert "access-control-allow-origin" not in {k.lower() for k in headers}
        del real


# --- the rest of the header policy -------------------------------------------


def test_api_responses_are_not_cached_to_disk(live):
    """Findings are a private repository's contents as read by a model. Cached,
    they outlive the server, the token and the session."""
    base, token, _port = live
    _s, headers, _b = _request(f"{base}/api/findings", token=token)
    assert headers.get("cache-control") == "no-store"


@needs_assets
def test_the_shell_carries_a_content_security_policy(live):
    """The strings this app renders are model output derived from a repository
    the user did not necessarily write, in a page that holds the token."""
    base, _token, _port = live
    _s, headers, _b = _request(f"{base}/")
    csp = headers.get("content-security-policy", "")
    assert "default-src 'none'" in csp
    assert "script-src 'self'" in csp
    assert "unsafe-inline" not in csp
    assert "frame-ancestors 'none'" in csp


@pytest.mark.parametrize("route", ["/api/findings", "/"])
def test_sniffing_and_referrers_are_off_everywhere(live, route):
    base, token, _port = live
    _s, headers, _b = _request(f"{base}{route}", token=token)
    assert headers.get("x-content-type-options") == "nosniff"
    assert headers.get("referrer-policy") == "no-referrer"


# --- the shell is exempt from the token, and must therefore hold no data ------


@needs_assets
def test_the_shell_is_served_without_a_token(live):
    """It has to be: the token arrives in the URL fragment, which is never sent
    to a server, so the first GET / cannot carry one. A gated shell 401s the
    request that would have loaded the script that reads the token."""
    base, _token, _port = live
    status, _h, body = _request(f"{base}/")
    assert status == 200
    assert b"<div id=\"root\">" in body


@needs_assets
def test_the_unauthenticated_shell_contains_no_store_data(live, project):
    """What makes the exemption safe rather than convenient.

    If the shell were ever templated with findings, the exemption would be a
    hole. It is static, so this asserts the bytes carry nothing from the store.
    """
    base, _token, _port = live
    _s, _h, body = _request(f"{base}/")
    text = body.decode("utf-8", "replace")
    for leak in ("run-0000000001", "guarded", str(project)):
        assert leak not in text


# --- the bind address --------------------------------------------------------


def test_the_socket_reports_a_loopback_address(live):
    """Read off a socket that is actually listening.

    NOT a grep for "0.0.0.0". That passes on `"0.0.0." + "0"`, on the empty
    string (which uvicorn treats as all interfaces), and on any variable -- it
    reads as a guard while guarding nothing.
    """
    del live
    import socket as socket_module

    sock = serve_module.bind()
    try:
        host, _port = sock.getsockname()
        assert host == "127.0.0.1"
        assert socket_module.inet_aton(host)[0] == 127
    finally:
        sock.close()


def test_the_running_server_does_not_answer_on_a_routable_interface(live):
    """The measurement the name above only implies.

    A previous version of this test created a probe socket, did nothing with
    it, and asserted `BIND_HOST == "127.0.0.1"` -- which is a restatement of a
    constant the AST test below already pins, dressed as a runtime check.

    This connects to the SAME PORT on this machine's routable address. Bound to
    loopback, that is refused; bound to 0.0.0.0, it would be accepted and the
    control plane would be reachable from the network.
    """
    _base, _token, port = live
    import socket as socket_module

    routable = _routable_address()
    if routable is None:  # pragma: no cover - a host with no non-loopback IPv4
        pytest.skip("this machine has no routable IPv4 address to probe")

    probe = socket_module.socket()
    probe.settimeout(5)
    try:
        with pytest.raises(OSError):
            probe.connect((routable, port))
    finally:
        probe.close()


def _routable_address() -> str | None:
    """This machine's non-loopback IPv4, without sending anything.

    A connected UDP socket picks a source address from the routing table and
    transmits nothing, so this works offline and touches no network.
    """
    import socket as socket_module

    probe = socket_module.socket(socket_module.AF_INET, socket_module.SOCK_DGRAM)
    try:
        probe.connect(("203.0.113.1", 9))  # TEST-NET-3, reserved, unroutable
        host = probe.getsockname()[0]
    except OSError:
        return None
    finally:
        probe.close()
    return None if host.startswith("127.") else host


def test_the_bind_host_is_a_literal_with_no_configuration_path():
    """An AST assertion, because the invariant is that nothing can change it."""
    source = Path(serve_module.__file__).read_text(encoding="utf-8")
    assignments = [
        node
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "BIND_HOST" for t in node.targets
        )
    ]
    assert len(assignments) == 1
    assert isinstance(assignments[0].value, ast.Constant)
    assert assignments[0].value.value == "127.0.0.1"


# --- the token itself --------------------------------------------------------


def test_the_token_is_long_and_comes_from_the_csprng():
    """`hmac.compare_digest` is a correct comparison of a value whose strength
    is otherwise unspecified. 32 bytes, urlsafe-base64, so >= 43 characters."""
    token = serve_module.mint_token()
    assert len(token) >= 43
    assert token != serve_module.mint_token()


def test_the_minting_path_does_not_reach_for_a_weak_source():
    """A static scan, in the style the repo already uses elsewhere. `random`
    and `uuid` both produce plausible tokens that pass every "is it required"
    test while being guessable."""
    source = Path(serve_module.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        alias.name.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module.split(".")[0]
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
    }
    assert "random" not in imported
    assert "uuid" not in imported
    assert "secrets" in imported


# --- the read routes agree with the read model -------------------------------


def test_findings_are_returned_as_the_read_model_projects_them(live, project):
    base, token, _port = live
    _s, _h, body = _request(f"{base}/api/findings", token=token)
    payload = json.loads(body)

    assert payload["findings"] == []
    assert payload["run"]["run_id"] == "run-0000000001"
    assert payload["run"]["finished"] is True


def test_the_config_route_reports_the_ceiling_caveats(live):
    """A ceiling rendered without them is a bound the user believes they have."""
    base, token, _port = live
    _s, _h, body = _request(f"{base}/api/config", token=token)
    payload = json.loads(body)
    assert any("bounds how many runs" in c for c in payload["caveats"])


def test_short_ids_use_the_same_prefix_length_as_the_cli():
    """PARITY, not a restatement of the constant.

    The previous body asserted `ID_PREFIX == 8`, which the name does not claim
    and which stays green while the CLI uses a different length. `decide`
    accepts the prefix `findings` prints, so the two have to agree or a user
    retypes an id the resolver will not match.
    """
    from whetstone import cli as cli_module

    assert cli_module._ID_PREFIX == ID_PREFIX


# --- the extra being ABSENT, which CI otherwise never exercises ---------------
#
# `ci.yml` runs `uv sync --all-extras`, so every leg has FastAPI installed and
# the "you need the ui extra" path is unreachable there -- a check that quietly
# does not run, in a milestone whose constraints demand every test be forced
# red first. A meta_path blocker reaches it without a matrix change.


class _BlockImport:
    """Make named top-level packages un-importable for the duration."""

    def __init__(self, *names: str) -> None:
        self.names = set(names)

    def find_spec(self, fullname, _path=None, _target=None):
        if fullname.split(".")[0] in self.names:
            raise ImportError(f"blocked for this test: {fullname}")
        return None


@pytest.fixture
def without_fastapi(monkeypatch):
    import sys

    for name in list(sys.modules):
        if name.split(".")[0] in {"fastapi", "starlette", "uvicorn"}:
            monkeypatch.delitem(sys.modules, name, raising=False)
    blocker = _BlockImport("fastapi", "starlette", "uvicorn")
    monkeypatch.setattr(sys, "meta_path", [blocker, *sys.meta_path])
    return blocker


def test_a_missing_ui_extra_names_the_install_rather_than_the_module(
    without_fastapi, project
):
    """`ModuleNotFoundError: fastapi` is a true statement that tells a user
    nothing about what to type. Same split `browser.py` already draws between
    a missing PACKAGE and a missing binary."""
    from whetstone.errors import WhetstoneError
    from whetstone.server.app import create_app

    del without_fastapi
    with pytest.raises(WhetstoneError) as caught:
        create_app(
            config=load_config(project / "whetstone.yaml"),
            project_root=project,
            state_root=project / ".state",
            token="x",
            port=1,
        )

    message = str(caught.value)
    assert "whetstone-cli[ui]" in message
    assert "ModuleNotFoundError" not in message


def test_the_cli_reports_a_missing_extra_as_an_error_not_a_traceback(
    without_fastapi, project
):
    from typer.testing import CliRunner

    from whetstone.cli import app as cli_app

    del without_fastapi
    result = CliRunner().invoke(cli_app, ["ui", "--path", str(project)])

    assert result.exit_code == 1
    assert "whetstone-cli[ui]" in result.output
    assert "Traceback" not in result.output


# --- what the terminal actually prints ----------------------------------------


def test_no_announced_line_contains_a_control_character():
    """MEASURED, and the escaping that caused it is correct.

    `cli.py` passes every announced line through `_printable`, which renders
    control characters visible so a model-authored or repo-read string cannot
    retitle the reader's window. It does not make an exception for a newline
    this module put there itself, so a multi-line announce string printed the
    URL with a trailing literal `\x0a` -- ugly, and a character a user copying
    the line would take with them into their address bar.

    The fix is one call per line. This asserts the shape rather than the fix,
    so re-embedding a newline anywhere in `serve()` fails here.
    """
    lines: list[str] = []
    with pytest.raises(_Stop):
        serve_module.serve(
            config=_bare_config(),
            project_root=Path("."),
            state_root=Path("."),
            open_browser=False,
            show_url=True,
            announce=lambda line: lines.append(line) or _raise_when_done(lines),
        )

    assert lines, "serve() announced nothing"
    for line in lines:
        assert "\n" not in line, repr(line)
        assert "\r" not in line, repr(line)


class _Stop(Exception):
    """Ends `serve()` before it blocks on uvicorn."""


def _raise_when_done(lines: list[str]) -> None:
    # Three lines is everything `serve()` says before it starts serving.
    if len(lines) >= 3:
        raise _Stop


def _bare_config():
    from whetstone.config.model import ProjectConfig, WhetstoneConfig

    return WhetstoneConfig(project=ProjectConfig(name="announce"))


def test_the_token_is_absent_from_the_terminal_unless_asked_for():
    """A terminal is not a private surface -- scrollback is screen-shared,
    piped through `tee`, and pasted into chat windows."""
    lines: list[str] = []
    with pytest.raises(_Stop):
        serve_module.serve(
            config=_bare_config(),
            project_root=Path("."),
            state_root=Path("."),
            open_browser=False,
            show_url=False,
            announce=lambda line: lines.append(line) or _raise_when_done(lines),
        )

    assert lines
    assert not any("#t=" in line for line in lines), lines
    assert any("--print-url" in line for line in lines)
