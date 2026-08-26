"""Deciding and running from the control plane.

TWO SURFACES, ONE STORE, and this file is where that stops being a claim about
reads. A decision made in the browser has to be the decision `whetstone
findings` then shows, and a run started in the browser has to contend with
`whetstone run` in a terminal rather than racing it.
"""

from __future__ import annotations

import contextlib
import json
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest
from typer.testing import CliRunner

# NOT `pytest.importorskip`. That skips the WHOLE MODULE when the extra
# is absent -- and on a CI leg that dropped `--all-extras`, every test in
# here would skip while the leg stayed green. `_bundle` raises instead
# wherever CI is set, and skips only on a developer machine.
from _bundle import UI_EXTRA_MISSING  # noqa: E402
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

pytestmark = pytest.mark.skipif(
    UI_EXTRA_MISSING is not None, reason=UI_EXTRA_MISSING or "ui extra present"
)


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
    with contextlib.closing(connect(tmp_path / ".state")) as conn:
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
    with contextlib.closing(connect(project / ".state")) as conn:
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

    with contextlib.closing(connect(project / ".state")) as conn:
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

    with (
        contextlib.closing(connect(project / ".state")) as conn,
        pytest.raises(WhetstoneError) as caught,
    ):
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


@pytest.mark.parametrize(
    ("hostile", "expected"),
    [
        # Reaches the route and is REFUSED BY VALIDATION -- these are single
        # path segments, so routing matches and the handler runs.
        ("..", 400),
        ("a" * 31, 400),
        ("Z" * 32, 400),
        ("0" * 32 + chr(10), 400),
        # ...and never reaches it, because the slashes make it a different
        # path. A 404 here is routing, NOT the ticket check.
        ("../../etc/passwd", 404),
        ("", 404),
    ],
    ids=["dotdot", "too-short", "uppercase", "trailing-newline", "traversal", "empty"],
)
def test_a_ticket_is_not_a_path(live, hostile, expected):
    """`/api/runs/<ticket>/events` joins the ticket to a filesystem path.

    THE STATUS IS ASSERTED PER CASE. The first version accepted `status in
    (400, 404)` for every input, which let ROUTING stand in for VALIDATION: a
    case that 404s because the URL has slashes in it proves nothing about the
    ticket check, and the two are told apart only by which number comes back.

    `"0" * 32 + chr(10)` is the case that motivated `fullmatch`: Python's `$`
    matches before a trailing newline, so `match(r"^[0-9a-f]{32}$")` accepted
    it.
    """
    base, token, _project = live
    url = f"{base}/api/runs/{urllib.request.quote(hostile, safe='')}/events"

    status, _body = _call(url, token=token)

    assert status == expected, (hostile, status)


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

    with contextlib.closing(connect(tmp_path / ".state")) as conn:
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


# --- what the review round changed --------------------------------------------


def test_a_ticket_with_a_trailing_newline_is_not_a_ticket(tmp_path):
    """Python's `$` matches BEFORE a trailing newline.

    `re.match(r"^[0-9a-f]{32}$", "0"*32 + chr(10))` succeeds, so the anchored
    pattern accepted a value that is not a ticket -- in the check whose entire
    job is deciding what a ticket is. `fullmatch` is the fix; this is the case
    that distinguishes it from the version that shipped.
    """
    with pytest.raises(runs_module.UnknownRunError):
        runs_module.events_path(tmp_path, "0" * 32 + chr(10))


def test_the_run_lock_is_released_when_the_thread_cannot_start(project, monkeypatch):
    """Released DETERMINISTICALLY, not eventually.

    The lock is taken in `start()` and released by the worker's `finally`. If
    `Thread.start()` raises -- which it does when the process cannot create
    another thread -- no worker ever runs and nothing runs that `finally`.

    A first version of this test checked the lock AFTER the `pytest.raises`
    block and passed with the fix removed, which is what a mutation battery is
    for. The reason is worth writing down: CPython collects the abandoned
    `ExitStack`, collecting the suspended `run_lock` generator, which raises
    `GeneratorExit` at its `yield` and runs the `finally` after all. So the
    lock does come back -- once the exception's traceback stops referencing the
    frame that holds the stack, which is not a moment anyone can name.

    So the check happens INSIDE the `except`, while the traceback is still
    alive and collection cannot have happened. That is the difference between
    releasing it and hoping.
    """
    state = project / ".state"

    def _refuse(self):
        raise RuntimeError("cannot start new thread")

    monkeypatch.setattr(threading.Thread, "start", _refuse)

    try:
        runs_module.start(
            config=load_config(project / "whetstone.yaml"),
            project_root=project,
            state_root=state,
            tier="quick",
            changed_only=False,
        )
    except RuntimeError:
        # The traceback is live here, so the ExitStack cannot have been
        # collected. If `start()` did not close it explicitly, the lock is
        # still held and this raises RunInProgressError.
        with run_lock(state):
            pass
    else:  # pragma: no cover - the monkeypatch guarantees the raise
        raise AssertionError("Thread.start was expected to raise")


@pytest.mark.parametrize("body", ["[]", '"just a string"', "12", "null"])
def test_a_body_that_is_not_an_object_gets_this_api_s_own_sentence(live, body):
    """FastAPI validates a DECLARED body model before the handler runs.

    With `body: dict[str, Any]` on the signature, a caller sending `[]` got
    FastAPI's 422 validation blob and the `isinstance` guard inside the
    function was dead code. The body is read in the handler now, so every
    refusal on this route is one sentence written for a human.
    """
    base, token, project = live
    request = urllib.request.Request(
        f"{base}/api/findings/{_first_id(project)}/decide",
        data=body.encode(),
        method="POST",
    )
    request.add_header(TOKEN_HEADER, token)
    request.add_header("Content-Type", "application/json")

    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=60)

    assert caught.value.code == 400
    payload = json.loads(caught.value.read())
    assert "JSON object" in payload["error"]


def test_a_body_that_is_not_json_at_all_says_so(live):
    base, token, project = live
    request = urllib.request.Request(
        f"{base}/api/findings/{_first_id(project)}/decide",
        data=b"{ truncated",
        method="POST",
    )
    request.add_header(TOKEN_HEADER, token)
    request.add_header("Content-Type", "application/json")

    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(request, timeout=60)

    assert caught.value.code == 400
    assert "not valid JSON" in json.loads(caught.value.read())["error"]


def test_starting_a_run_with_no_body_at_all_is_the_ordinary_case(live):
    """The button sends `{}`; `curl -X POST` sends nothing. Both are defaults."""
    base, token, _project = live
    request = urllib.request.Request(f"{base}/api/runs", data=b"", method="POST")
    request.add_header(TOKEN_HEADER, token)

    with urllib.request.urlopen(request, timeout=60) as response:
        assert response.status == 200
        assert len(json.loads(response.read())["ticket"]) == 32


def test_a_reader_that_goes_away_stops_the_tail(tmp_path):
    """A disconnected client must not hold a worker for the rest of a run.

    Cancellation does not stop a SYNC generator running in a threadpool -- it
    only stops whoever was reading it -- so `tail` takes a `stop` event and the
    route sets it when the response is torn down. Asserted on `tail` directly:
    driving a real disconnect through uvicorn would be timing-dependent, and
    the flag is the mechanism either way.
    """
    events = tmp_path / "events" / f"{runs_module.new_ticket()}.jsonl"
    events.parent.mkdir(parents=True)
    events.write_text(json.dumps({"kind": "run_started"}) + "\n", encoding="utf-8")

    stop = threading.Event()
    stream = runs_module.tail(events, stop=stop)

    assert next(stream)["kind"] == "run_started"
    stop.set()
    with pytest.raises(StopIteration):
        next(stream)


def test_a_complete_line_that_is_not_json_ends_the_stream_with_an_error(tmp_path):
    """The behaviour the docstring used to describe wrongly.

    It said such a line was "treated as not yet complete and retried", which
    the code has never done. A terminated line is not a partial write -- it is
    a flushed line that is not JSON, which is a bug in the writer.
    """
    events = tmp_path / "events" / f"{runs_module.new_ticket()}.jsonl"
    events.parent.mkdir(parents=True)
    events.write_text("not json at all\n", encoding="utf-8")

    collected = list(runs_module.tail(events))

    assert len(collected) == 1
    assert collected[0]["kind"] == "error"
    assert "unreadable event" in collected[0]["error"]
