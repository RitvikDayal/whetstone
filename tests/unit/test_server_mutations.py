"""Deciding and running from the control plane.

TWO SURFACES, ONE STORE, and this file is where that stops being a claim about
reads. A decision made in the browser has to be the decision `whetstone
findings` then shows, and a run started in the browser has to contend with
`whetstone run` in a terminal rather than racing it.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from typer.testing import CliRunner

from whetstone.cli import app as cli_app
from whetstone.config.loader import load_config
from whetstone.grade import Grade
from whetstone.lenses.base import Candidate, Evidence, EvidenceKind
from whetstone.queue.dispositions import Disposition
from whetstone.queue.dispositions import apply as apply_disposition
from whetstone.runlock import run_lock
from whetstone.server import runs as runs_module
from whetstone.server import serve as serve_module
from whetstone.server.security import TOKEN_HEADER
from whetstone.severity import Severity
from whetstone.store.db import connect
from whetstone.store.findings import list_findings, upsert

pytest.importorskip("fastapi", reason="the ui extra is not installed")


def _candidate(rule_id: str, subject: str) -> Candidate:
    return Candidate(
        lens="code-defects",
        rule_id=rule_id,
        subject=subject,
        title=f"something wrong in {subject}",
        detail="detail",
        severity=Severity("high"),
        evidence=Evidence(kind=EvidenceKind.metric, summary="seeded", data={}),
        grade=Grade("A"),
        grade_reason="graded A",
    )


# UNAMBIGUOUSLY IN THE PAST, and the date is the point rather than an arbitrary
# constant. `get_last_run` orders by `started_at DESC`, so a seeded run stamped
# with today's date at 10:00 is the "most recent" run for anyone running the
# suite before 10:00 UTC -- and a run started live during the test then sorts
# BEHIND the fixture. That is not a defect in the ordering, it is a fixture
# claiming to have happened in the future, and it made
# `test_every_event_restates_something_the_store_also_holds` fail for a reason
# that had nothing to do with what it measures.
_SEEDED_AT = "2020-01-01T00:00:00+00:00"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    (tmp_path / "whetstone.yaml").write_text(
        "version: 1\nproject:\n  name: mutable\nstate_dir: .state\n",
        encoding="utf-8",
    )
    with connect(tmp_path / ".state") as conn:
        conn.execute(
            "INSERT INTO runs (id, tier, scope_mode, file_count, started_at, "
            "status, skipped_json) VALUES ('run-0000000001','quick','full',1,"
            "'" + _SEEDED_AT + "','complete','[]')"
        )
        for index in range(3):
            upsert(
                conn,
                _candidate(f"r{index}", f"app/{index}.py"),
                "run-0000000001",
                _SEEDED_AT,
            )
    return tmp_path


@pytest.fixture
def live(project: Path):
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
    base = f"http://127.0.0.1:{port}"
    deadline = time.time() + 30
    while time.time() < deadline:
        try:
            _call(f"{base}/api/findings", token=token)
            break
        except Exception:  # noqa: BLE001 - not listening yet
            time.sleep(0.05)
    else:  # pragma: no cover - the server never came up
        raise AssertionError("the control plane never started listening")
    try:
        yield base, token, project
    finally:
        server.should_exit = True
        thread.join(timeout=30)


def _call(url: str, *, token: str | None = None, payload=None, method="GET"):
    data = None if payload is None else json.dumps(payload).encode()
    request = urllib.request.Request(url, data=data, method=method)
    if token is not None:
        request.add_header(TOKEN_HEADER, token)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read() or b"null")
    except urllib.error.HTTPError as exc:
        body = exc.read()
        try:
            return exc.code, json.loads(body or b"null")
        except ValueError:
            return exc.code, {"raw": body.decode("utf-8", "replace")}


def _first_id(project: Path) -> str:
    with connect(project / ".state") as conn:
        return list_findings(conn, state="queued")[0].id


# --- deciding ----------------------------------------------------------------


def test_a_decision_made_in_the_browser_is_the_one_the_cli_shows(live):
    """The whole claim of this milestone, exercised across both surfaces."""
    base, token, project = live
    finding_id = _first_id(project)

    status, body = _call(
        f"{base}/api/findings/{finding_id}/decide",
        token=token,
        payload={"disposition": "verify"},
        method="POST",
    )
    assert status == 200, body
    assert body["state"] == "verified"

    result = CliRunner().invoke(
        cli_app, ["findings", "--state", "verified", "--path", str(project)]
    )
    assert result.exit_code == 0
    assert finding_id[:8] in result.output


def test_rejecting_without_confirming_is_refused(live):
    """`reject` is the one decision a later run cannot undo, and the CLI asks
    first. A browser has no prompt, so the caller must have already decided."""
    base, token, project = live
    finding_id = _first_id(project)

    status, body = _call(
        f"{base}/api/findings/{finding_id}/decide",
        token=token,
        payload={"disposition": "reject", "reason": "not worth it"},
        method="POST",
    )

    assert status == 400
    assert "permanent" in body["error"]

    with connect(project / ".state") as conn:
        assert list_findings(conn, state="queued")


def test_rejecting_with_confirmation_is_recorded(live):
    base, token, project = live
    finding_id = _first_id(project)

    status, body = _call(
        f"{base}/api/findings/{finding_id}/decide",
        token=token,
        payload={
            "disposition": "reject",
            "reason": "a deliberate design choice",
            "confirm": True,
        },
        method="POST",
    )

    assert status == 200, body
    assert body["state"] == "rejected"


def test_the_argument_rules_come_back_in_dispositions_own_words(live):
    """VERBATIM, asserted rather than promised.

    `queue/dispositions.py` writes the sentence that names the missing argument
    AND why it is required. A second copy of that rule in the web layer is a
    copy that will drift -- the same lesson `autonomy.py` records about its own
    duplicated classification. This compares the API's text against the text
    the library itself produces for the identical call.
    """
    base, token, project = live
    finding_id = _first_id(project)

    _status, body = _call(
        f"{base}/api/findings/{finding_id}/decide",
        token=token,
        payload={"disposition": "reject", "confirm": True},
        method="POST",
    )

    from whetstone.errors import WhetstoneError

    with connect(project / ".state") as conn, pytest.raises(WhetstoneError) as caught:
        apply_disposition(
            conn,
            finding_id,
            Disposition.reject,
            reason=None,
            wake=None,
            assignee=None,
            now="2020-01-01T00:00:00+00:00",
        )

    assert body["error"] == str(caught.value)


def test_an_unknown_disposition_is_a_usage_error_not_a_server_error(live):
    base, token, project = live
    status, body = _call(
        f"{base}/api/findings/{_first_id(project)}/decide",
        token=token,
        payload={"disposition": "obliterate"},
        method="POST",
    )
    assert status == 400
    assert "obliterate" in body["error"]


def test_deciding_on_a_finding_that_does_not_exist_says_so(live):
    base, token, _project = live
    status, body = _call(
        f"{base}/api/findings/{'0' * 32}/decide",
        token=token,
        payload={"disposition": "verify"},
        method="POST",
    )
    assert status == 400
    assert "no finding" in body["error"]


def test_deciding_requires_the_token_like_every_other_route(live):
    base, _token, project = live
    status, _body = _call(
        f"{base}/api/findings/{_first_id(project)}/decide",
        payload={"disposition": "verify"},
        method="POST",
    )
    assert status == 401


# --- running -----------------------------------------------------------------


def test_a_run_is_refused_while_the_cli_holds_the_lock(live):
    """SINGLE-FLIGHT ACROSS PROCESSES, which is the case that matters.

    An in-process lock would guard browser-against-browser and miss
    browser-against-terminal -- and the second is the one `store/findings.py`
    documents as the race, and the one M3 makes more likely by putting a second
    surface in front of the same project.
    """
    base, token, project = live

    with run_lock(project / ".state"):
        status, body = _call(f"{base}/api/runs", token=token, payload={}, method="POST")

    assert status == 409
    assert "already holds" in body["error"]


def test_the_run_lock_is_released_and_a_run_is_accepted_afterwards(live):
    """The counterweight: a 409 that never clears is a broken tool, not a lock."""
    base, token, project = live

    with run_lock(project / ".state"):
        assert _call(f"{base}/api/runs", token=token, payload={}, method="POST")[0] == 409

    status, body = _call(f"{base}/api/runs", token=token, payload={}, method="POST")
    assert status == 200, body
    assert len(body["ticket"]) == 32


def test_a_ticket_is_not_a_path(live):
    """`/api/runs/<ticket>/events` joins the ticket to a filesystem path."""
    base, token, _project = live
    for hostile in ("..", "../../etc/passwd", "a" * 31, "Z" * 32, ""):
        url = f"{base}/api/runs/{urllib.request.quote(hostile, safe='')}/events"
        status, _body = _call(url, token=token)
        assert status in (400, 404), (hostile, status)


def test_the_event_stream_replays_from_the_start_and_terminates(live):
    """A client that connects late must still see the whole run.

    The file IS the stream -- there is no subscriber registry and no in-memory
    fan-out -- so replay and live are the same code path, and a second tab, a
    reconnect and a late connect are not three cases anybody has to remember.
    """
    base, token, project = live

    status, body = _call(f"{base}/api/runs", token=token, payload={}, method="POST")
    assert status == 200, body
    ticket = body["ticket"]

    events = _read_stream(f"{base}/api/runs/{ticket}/events", token)

    kinds = [event["kind"] for event in events]
    assert kinds[0] == "run_started"
    assert kinds[-1] in ("run_finished", "error")
    assert events[0]["run_id"].startswith("run-")


def test_a_second_reader_of_a_finished_run_sees_the_same_events(live):
    base, token, _project = live
    _status, body = _call(f"{base}/api/runs", token=token, payload={}, method="POST")
    ticket = body["ticket"]

    first = _read_stream(f"{base}/api/runs/{ticket}/events", token)
    second = _read_stream(f"{base}/api/runs/{ticket}/events", token)

    assert first == second


def test_every_event_restates_something_the_store_also_holds(live):
    """The stream is a convenience, never the record.

    A client that misses every event and reloads must see the same state, so
    the run the stream announced has to be the run the store then reports.
    """
    base, token, project = live
    _status, body = _call(f"{base}/api/runs", token=token, payload={}, method="POST")
    events = _read_stream(f"{base}/api/runs/{body['ticket']}/events", token)

    announced = events[0]["run_id"]
    _status, findings = _call(f"{base}/api/findings", token=token)
    assert findings["run"]["run_id"] == announced

    finished = [e for e in events if e["kind"] == "run_finished"]
    assert finished
    assert finished[0]["status"] == findings["run"]["status"]
    assert finished[0]["skips"] == findings["run"]["skips"]
    del project


def _read_stream(url: str, token: str, *, limit: int = 400) -> list[dict]:
    """Consume an SSE stream to its end and return the decoded events."""
    request = urllib.request.Request(url)
    request.add_header(TOKEN_HEADER, token)
    events: list[dict] = []
    with urllib.request.urlopen(request, timeout=120) as response:
        for raw in response:
            line = raw.decode("utf-8").strip()
            if not line.startswith("data: "):
                continue
            events.append(json.loads(line[len("data: ") :]))
            if len(events) >= limit:  # pragma: no cover - runaway guard
                break
    return events


# --- the ticket, on its own ---------------------------------------------------


def test_events_path_refuses_anything_that_is_not_a_ticket(tmp_path):
    for hostile in ("..", "../evil", "abc", "g" * 32, "A" * 32, ""):
        with pytest.raises(runs_module.UnknownRunError):
            runs_module.events_path(tmp_path, hostile)


def test_events_path_accepts_a_real_ticket(tmp_path):
    ticket = runs_module.new_ticket()
    path = runs_module.events_path(tmp_path, ticket)
    assert path.parent == (tmp_path / "events").resolve()
    assert path.name == f"{ticket}.jsonl"


def test_a_ticket_is_long_enough_not_to_be_guessed():
    ticket = runs_module.new_ticket()
    assert len(ticket) == 32
    assert ticket != runs_module.new_ticket()


def test_the_containment_check_holds_even_if_the_pattern_is_loosened(
    tmp_path, monkeypatch
):
    """The second layer, tested by removing the first.

    A mutation battery found this branch unreachable: the ticket pattern is
    strict enough that nothing hostile ever reaches the containment check, so
    deleting the check broke no test. That makes it defence-in-depth with
    nothing measuring it -- which is how a guard rots into a comment. The
    pattern is what actually stops a traversal; the containment check is what
    still stops one after somebody widens the pattern, so it is tested under
    exactly that condition.
    """
    import re as re_module

    monkeypatch.setattr(runs_module, "_TICKET", re_module.compile(r"^.*$"))

    with pytest.raises(runs_module.UnknownRunError) as caught:
        runs_module.events_path(tmp_path, "../../escaped")

    assert "does not resolve inside" in str(caught.value)


def test_a_progress_sink_that_raises_does_not_end_the_run(tmp_path):
    """A run destroyed by the thing WATCHING it.

    The sink belongs to a surface -- today an SSE stream -- and a surface is
    not allowed to be load bearing. Every event also lands in the store or in
    `result.skips`, so a sink that raises costs liveness and no information.
    Nothing tested that until a mutation removing the guard stayed green.
    """
    from whetstone.runner import execute_run

    (tmp_path / "whetstone.yaml").write_text(
        "version: 1\nproject:\n  name: hostile-sink\nstate_dir: .state\n",
        encoding="utf-8",
    )
    config = load_config(tmp_path / "whetstone.yaml")
    calls: list[str] = []

    def explode(event):
        calls.append(event["kind"])
        raise RuntimeError("the watcher is broken")

    with connect(tmp_path / ".state") as conn:
        result = execute_run(
            conn,
            config,
            tmp_path,
            tmp_path / ".state",
            tier="quick",
            changed_only=False,
            on_event=explode,
        )

    assert result.status == "complete"
    # And it was actually called -- otherwise this passes against a run that
    # never emitted anything, which is the vacuous version of the same test.
    assert "run_started" in calls
    assert "run_finished" in calls


@pytest.mark.parametrize("token_value", [None, "wrong-token"])
def test_a_refused_post_gets_its_status_and_not_a_connection_reset(live, token_value):
    """MEASURED, on Windows, on this exact route.

    Answering a POST without consuming its request body leaves the client still
    writing into a socket the server is closing, and the client raises
    ConnectionResetError instead of reading the 401 that was actually sent. The
    SPA would have rendered "network error" for a plain expired token -- the
    least actionable message available -- and the cause is not visible from
    either side.
    """
    base, _token, project = live

    status, body = _call(
        f"{base}/api/findings/{_first_id(project)}/decide",
        token=token_value,
        payload={"disposition": "verify", "padding": "x" * 20000},
        method="POST",
    )

    assert status == 401
    assert "token" in body["error"].lower()


def test_a_post_refused_by_the_host_check_also_gets_its_status(live):
    """The other refusal path, which forgot the same thing."""
    base, token, project = live

    request = urllib.request.Request(
        f"{base}/api/findings/{_first_id(project)}/decide",
        data=json.dumps({"disposition": "verify", "padding": "x" * 20000}).encode(),
        method="POST",
    )
    request.add_header(TOKEN_HEADER, token)
    request.add_header("Content-Type", "application/json")
    request.add_header("Host", "evil.com")

    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=60)

    assert caught.value.code == 403
