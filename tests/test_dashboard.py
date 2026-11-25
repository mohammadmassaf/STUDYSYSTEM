"""The dashboard route (3.1; D-17, D-161, D-162): the same services the tools call, behind HTTP.
Each write is his 40 minutes on I3302, sent as the dashboard would send it."""

import concurrent.futures
import datetime
import http.client
import importlib.metadata
import os
import socket
import sqlite3
import subprocess
import sys
import time
import urllib.request

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from starlette.testclient import TestClient

from studysystem import dashboard
from studysystem.dashboard import create_app
from studysystem.db import migrate
from studysystem.db.tables import capacity, hours_entry, study_session
from studysystem.services import dashboard_key, launcher, study_loop
from studysystem.services.courses import add_course
from studysystem.services.units import write_unit
from tests.test_plan import _chapter

SEM = "Semester 1 2026-2027"
OWN = "http://127.0.0.1:8765"


@pytest.fixture
def web(service_engine, user_id):
    return add_course(service_engine, user_id, "I3302", "Server-Side Web Development", SEM)


@pytest.fixture
def client(service_engine, user_id):
    return TestClient(create_app(service_engine), base_url=OWN)


def entries(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(hours_entry)).scalar_one()


def test_health_names_the_version_and_the_schema(client):
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {
        "server_version": importlib.metadata.version("studysystem"),
        "schema_revision": None,  # the test schema is built by create_all, not migrated
    }


def test_the_page_is_served_at_the_root_and_ranks_nothing(client):
    """3.3 (D-19, D-186): the page comes from the same origin as /state, reads the server's plan,
    and holds no ranking code of its own."""
    response = client.get("/")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/html")
    page = response.text
    assert 'fetch("/state"' in page
    for gone in ("const DATA", "const QUEUE", "function buildPlan"):
        assert gone not in page


def test_post_hours_logs_through_the_service(client, service_engine, web):
    response = client.post("/hours", json={"code": "I3302", "minutes": 40})

    assert response.status_code == 200
    body = response.json()
    assert body["minutes"] == 40
    with service_engine.connect() as conn:
        row = conn.execute(select(hours_entry)).one()
    assert row.id == body["hours_entry_id"]
    assert row.minutes == 40


def test_post_hours_takes_a_chapter(client, service_engine, user_id, web):
    # The route's fields are the service's: a chapter's minutes pass through, as a topic's do.
    with write_unit(service_engine) as conn:
        chapter = _chapter(conn, user_id, web["course_id"])

    response = client.post("/hours", json={"code": "I3302", "minutes": 40, "material_id": chapter})

    assert response.status_code == 200
    with service_engine.connect() as conn:
        assert conn.execute(select(hours_entry.c.material_id)).scalar_one() == chapter


def test_an_error_has_the_shape_the_tool_returns(client, call, web):
    response = client.post("/hours", json={"code": "I3302", "minutes": 0})
    tool = call("study_log_hours", {"code": "I3302", "minutes": 0})

    assert response.status_code == 400
    assert response.json() == tool.structured_content
    assert response.json()["code"] == "invalid_value"


def test_an_unknown_course_is_404(client, service_engine, web):
    response = client.post("/hours", json={"code": "I9999", "minutes": 40})

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"
    assert entries(service_engine) == 0


def test_a_mistyped_field_is_refused_not_dropped(client, service_engine, web):
    response = client.post("/hours", json={"code": "I3302", "minutes": 40, "topic": "MySQL"})

    assert response.status_code == 400
    assert response.json()["field_errors"] == [
        {"field": "topic", "problem": "is not a field of POST /hours"}
    ]
    assert entries(service_engine) == 0


@pytest.mark.parametrize(
    ("body", "field"),
    [({"minutes": 40}, "code"), ({"code": "I3302", "minutes": 40, "topic_id": 7}, "topic_id")],
)
def test_a_missing_code_or_a_non_text_id_is_refused(client, service_engine, web, body, field):
    response = client.post("/hours", json=body)

    assert response.status_code == 400
    assert response.json()["field_errors"][0]["field"] == field
    assert entries(service_engine) == 0


@pytest.mark.parametrize("content", ["not json", "[40]"])
def test_a_body_that_is_not_one_json_object_is_refused(client, service_engine, web, content):
    response = client.post("/hours", content=content, headers={"content-type": "application/json"})

    assert response.status_code == 400
    assert response.json()["field_errors"][0]["field"] == "body"
    assert entries(service_engine) == 0


@pytest.mark.parametrize("path", ["/hours", "/sessions", "/sessions/stop", "/capacity"])
def test_a_write_that_is_not_json_is_refused(client, service_engine, web, path):
    """What a page on another site can send without asking first: a form, or text/plain."""
    response = client.post(
        path,
        content='{"code": "I3302", "minutes": 720}',
        headers={"content-type": "text/plain"},
    )

    assert response.status_code == 415
    assert response.json()["code"] == "not_json"
    assert entries(service_engine) == 0


def test_the_permission_check_before_a_cross_site_write_is_never_granted(client):
    response = client.options(
        "/hours",
        headers={
            "origin": "https://example.com",
            "access-control-request-method": "POST",
            "access-control-request-headers": "content-type",
        },
    )

    assert "access-control-allow-origin" not in response.headers


@pytest.mark.parametrize("path", ["/health", "/hours"])
def test_a_request_to_another_host_name_is_refused(service_engine, web, path):
    """A site that re-pointed its own name to 127.0.0.1 still sends that name."""
    other = TestClient(create_app(service_engine), base_url="http://evil.example:8765")

    if path == "/health":
        response = other.get(path)
    else:
        response = other.post(path, json={"code": "I3302", "minutes": 720})

    assert response.status_code == 403
    assert response.json()["code"] == "wrong_host"
    assert entries(service_engine) == 0


def test_localhost_is_our_own_host_too(service_engine, user_id):
    local = TestClient(create_app(service_engine), base_url="http://localhost:8765")

    assert local.get("/health").status_code == 200


def test_a_locked_database_is_503(client, monkeypatch, web):
    def locked(*_args, **_kwargs):
        raise OperationalError("INSERT ...", {}, sqlite3.OperationalError("database is locked"))

    monkeypatch.setattr(study_loop, "write_unit", locked)

    response = client.post("/hours", json={"code": "I3302", "minutes": 40})

    assert response.status_code == 503
    assert response.json()["code"] == "database_busy"


# --- the timer (3.5; D-198): start and stop through the route --------------------------------


def _clock_after(monkeypatch, started_at: str, **span) -> None:
    start = datetime.datetime.fromisoformat(started_at)
    stamp = (start + datetime.timedelta(**span)).strftime(study_loop.TIMESTAMP)
    monkeypatch.setattr(study_loop, "now", lambda: stamp)


def test_a_timed_session_through_the_route_logs_timer_hours(
    client, service_engine, web, monkeypatch
):
    started = client.post("/sessions", json={"code": "I3302", "mode": "practice"})
    assert started.status_code == 200
    session = started.json()
    _clock_after(monkeypatch, session["started_at"], minutes=40)

    stopped = client.post(
        "/sessions/stop",
        json={"code": "I3302", "session_id": session["session_id"], "minutes": 40},
    )

    assert stopped.status_code == 200
    with service_engine.connect() as conn:
        row = conn.execute(select(hours_entry)).one()
    assert (row.id, row.minutes, row.source) == (stopped.json()["hours_entry_id"], 40, "timer")


def test_a_stop_after_four_hours_is_409_and_writes_nothing(
    client, service_engine, web, monkeypatch
):
    session = client.post("/sessions", json={"code": "I3302", "mode": "practice"}).json()
    _clock_after(monkeypatch, session["started_at"], hours=6)

    response = client.post(
        "/sessions/stop",
        json={"code": "I3302", "session_id": session["session_id"], "minutes": 360},
    )

    assert response.status_code == 409
    assert response.json()["code"] == "session_expired"
    assert entries(service_engine) == 0


def test_a_second_stop_is_409_and_the_first_entry_stands(client, service_engine, web, monkeypatch):
    session = client.post("/sessions", json={"code": "I3302", "mode": "practice"}).json()
    _clock_after(monkeypatch, session["started_at"], minutes=40)
    stop = {"code": "I3302", "session_id": session["session_id"], "minutes": 40}
    client.post("/sessions/stop", json=stop)

    response = client.post("/sessions/stop", json=stop)

    assert response.status_code == 409
    assert response.json()["code"] == "session_ended"
    assert entries(service_engine) == 1


def test_post_hours_cannot_claim_the_timer(client, service_engine, web):
    claimed = client.post("/hours", json={"code": "I3302", "minutes": 40, "source": "timer"})
    logged = client.post("/hours", json={"code": "I3302", "minutes": 40})

    assert claimed.status_code == 400
    assert claimed.json()["field_errors"][0]["field"] == "source"
    assert logged.status_code == 200
    with service_engine.connect() as conn:
        assert conn.execute(select(hours_entry.c.source)).scalars().all() == ["tool"]


@pytest.mark.parametrize(
    ("path", "body", "field"),
    [
        ("/sessions", {"code": "I3302"}, "mode"),
        ("/sessions", {"code": "I3302", "mode": "practice", "topic_id": "x"}, "topic_id"),
        ("/sessions/stop", {"code": "I3302", "minutes": 5}, "session_id"),
        ("/sessions/stop", {"code": "I3302", "session_id": "x"}, "minutes"),
        ("/sessions/stop", {"code": "I3302", "session_id": 7, "minutes": 5}, "session_id"),
    ],
)
def test_a_session_route_checks_its_fields(client, service_engine, web, path, body, field):
    response = client.post(path, json=body)

    assert response.status_code == 400
    assert response.json()["field_errors"][0]["field"] == field
    with service_engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(study_session)).scalar_one() == 0


# --- weekly hours from the page (D-290): the tool's service, behind POST /capacity ------------


def capacity_rows(engine) -> list[tuple[float, str]]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(capacity.c.hours_per_week, capacity.c.from_date).order_by(capacity.c.id)
        )
        return [tuple(r) for r in rows]


def test_post_capacity_adds_a_row_and_the_old_one_stays(client, service_engine, user_id):
    """His 10 h from 27 Sep, then 14 h from the Monday of this week, set on the page."""
    client.post("/capacity", json={"hours_per_week": 10, "from_date": "2026-09-27"})

    response = client.post("/capacity", json={"hours_per_week": 14, "from_date": "2026-11-09"})

    assert response.status_code == 200
    body = response.json()
    assert (body["hours_per_week"], body["from_date"]) == (14, "2026-11-09")
    assert capacity_rows(service_engine) == [(10, "2026-09-27"), (14, "2026-11-09")]


def test_post_capacity_errors_as_the_tool_does(client, call, service_engine, user_id):
    args = {"hours_per_week": 0, "from_date": "2026-11-09"}

    response = client.post("/capacity", json=args)
    tool = call("study_set_capacity", args)

    assert response.status_code == 400
    assert response.json() == tool.structured_content
    assert response.json()["field_errors"][0]["field"] == "hours_per_week"
    assert capacity_rows(service_engine) == []


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({"hours_per_week": 14}, "from_date"),
        ({"hours_per_week": 14, "from_date": 20261109}, "from_date"),
        ({"hours_per_week": True, "from_date": "2026-11-09"}, "hours_per_week"),
        ({"hours_per_week": 14, "from_date": "2026-11-09", "user_id": "x"}, "user_id"),
    ],
)
def test_post_capacity_checks_its_fields(client, service_engine, user_id, body, field):
    response = client.post("/capacity", json=body)

    assert response.status_code == 400
    assert response.json()["field_errors"][0]["field"] == field
    assert capacity_rows(service_engine) == []


# --- started_at on POST /hours (D-307) --------------------------------------------------------


def test_post_hours_takes_started_at(client, service_engine, web):
    response = client.post(
        "/hours", json={"code": "I3302", "minutes": 60, "started_at": "2026-11-01T20:00"}
    )

    assert response.status_code == 200
    expected = datetime.datetime(2026, 11, 1, 20, 0).astimezone(datetime.UTC)  # noqa: DTZ001
    with service_engine.connect() as conn:
        row = conn.execute(select(hours_entry)).one()
    assert row.occurred_at == expected.strftime(study_loop.TIMESTAMP)


def test_post_hours_refuses_a_bad_started_at_with_the_tools_error(
    client, call, service_engine, web
):
    response = client.post(
        "/hours", json={"code": "I3302", "minutes": 60, "started_at": "yesterday"}
    )
    tool = call("study_log_hours", {"code": "I3302", "minutes": 60, "started_at": "yesterday"})

    assert response.status_code == 400
    assert response.json() == tool.structured_content
    assert response.json()["field_errors"][0]["field"] == "started_at"
    assert entries(service_engine) == 0


def test_post_hours_refuses_a_non_text_started_at(client, service_engine, web):
    response = client.post("/hours", json={"code": "I3302", "minutes": 60, "started_at": 20261117})

    assert response.status_code == 400
    assert response.json()["field_errors"][0]["field"] == "started_at"
    assert entries(service_engine) == 0


# --- POST /shutdown: how `study stop` stops it (D-342) ------------------------------------------


@pytest.fixture
def stoppable(service_engine, user_id):
    """A client, and how often the app was told to stop."""
    stops = []
    app = create_app(service_engine, stop=lambda: stops.append(1))
    return TestClient(app, base_url=OWN), stops


def test_shutdown_stops_the_dashboard(stoppable):
    client, stops = stoppable

    response = client.post("/shutdown", json={})

    assert response.status_code == 200
    assert response.json() == {"stopping": True, "pid": os.getpid()}
    assert stops == [1]


def test_another_site_cannot_stop_the_dashboard(stoppable):
    client, stops = stoppable
    evil = TestClient(client.app, base_url="http://evil.example:8765")

    assert client.post("/shutdown", content="{}").json()["code"] == "not_json"
    assert client.get("/shutdown").status_code == 405
    assert evil.post("/shutdown", json={}).json()["code"] == "wrong_host"
    assert stops == []


def test_shutdown_answers_on_a_refused_schema(service_engine, user_id):
    """The upgrade it is for can leave the database behind head."""
    stops = []

    def refuse():
        raise migrate.SchemaBehindHead("0025", "0026")

    app = create_app(service_engine, refuse, lambda: stops.append(1))

    assert TestClient(app, base_url=OWN).post("/shutdown", json={}).status_code == 200
    assert stops == [1]


def test_a_stop_is_limited_to_eight_seconds(monkeypatch):
    """Longer than a write waits for a locked database, shorter than `study stop` waits (D-343)."""
    settings = {}
    monkeypatch.setattr(dashboard, "open_database", lambda: None)
    monkeypatch.setattr(dashboard.uvicorn, "run", lambda app, **kw: settings.update(kw))

    dashboard.main(8765)

    assert settings["timeout_graceful_shutdown"] == 8
    assert 5 < settings["timeout_graceful_shutdown"] < launcher.WAIT_SECONDS


@pytest.fixture
def real(tmp_path):
    """A dashboard process of its own, on a free port and the test's data folder. Yields its port
    and `stop`: `study stop`'s own code, then the process must have ended with code 0."""
    with socket.socket() as free:
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
    argv = [sys.executable, "-m", "studysystem.cli", "dashboard", "--port", str(port)]
    log = tmp_path / "dashboard.log"
    with log.open("wb") as out:
        process = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=out, stderr=out)

    def stop():
        try:
            assert launcher.stop_dashboard(port) == {"stopped": True}
            # on Windows the dashboard has ended, and the virtual environment's python, which
            # waits for it, ends just after; elsewhere only the port is sure to be free
            code = process.wait(timeout=2 if sys.platform == "win32" else 15)
        except (launcher.StudyError, subprocess.TimeoutExpired) as e:
            pytest.fail(f"{e!r}: {log.read_text()}")
        assert code == 0, log.read_text()

    try:
        deadline = time.monotonic() + 60
        while not launcher.answering(port):
            if time.monotonic() > deadline or process.poll() is not None:
                pytest.fail(f"no answer, exit {process.poll()}: {log.read_text()}")
            time.sleep(0.2)
        yield port, stop
    finally:
        process.kill()
        process.wait()


def test_a_real_dashboard_process_has_ended_when_stop_returns(real):
    """Clients that close without reading the reply could keep the process alive on Windows, not
    on every run (D-343)."""
    port, stop = real
    for _ in range(60):
        urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=5).close()

    stop()


def test_a_client_that_never_finishes_cannot_keep_the_process_alive(real):
    """On every run, unlike the closes above: without the limit, the stop waits for it forever."""
    port, stop = real
    client = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    client.putrequest("POST", "/capacity")
    client.putheader("Content-Type", "application/json")
    client.putheader("Content-Length", "100")
    client.putheader(dashboard_key.HEADER, dashboard_key.token("browser"))
    client.endheaders(b"{")  # 1 of its 100 bytes

    stop()

    client.close()


def test_a_save_that_waits_for_the_database_outlives_the_stop(real, isolated_data_dir):
    port, stop = real
    other = sqlite3.connect(isolated_data_dir / "studysystem.db", isolation_level=None)
    other.execute("BEGIN IMMEDIATE")  # another program writes, so the save has to wait
    client = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
    body = '{"hours_per_week": 11, "from_date": "2026-11-24"}'
    headers = {
        "Content-Type": "application/json",
        dashboard_key.HEADER: dashboard_key.token("browser"),
    }
    client.request("POST", "/capacity", body, headers)

    with concurrent.futures.ThreadPoolExecutor() as pool:
        stopping = pool.submit(stop)
        while launcher._listening(port) and not stopping.done():
            time.sleep(0.1)
        assert not stopping.done()  # the stop has begun, and it waits for the save
        other.execute("ROLLBACK")
        reply = client.getresponse()
        saved = (reply.status, reply.read())
        stopping.result()

    assert saved[0] == 200, saved[1]
    stored = other.execute("SELECT COUNT(*) FROM capacity WHERE hours_per_week = 11").fetchone()
    other.close()
    assert stored == (1,)


@pytest.mark.parametrize(
    ("method", "path"), [("GET", "/"), ("GET", "/health"), ("GET", "/nowhere"), ("POST", "/hours")]
)
def test_every_reply_forbids_other_pages_to_show_it(client, method, path):
    headers = client.request(method, path).headers

    assert headers["x-frame-options"] == "DENY"
    assert "frame-ancestors 'none'" in headers["content-security-policy"]
    assert headers["x-content-type-options"] == "nosniff"
    assert headers["cache-control"] == "no-store"


def test_a_refused_host_gets_the_headers_too(service_engine):
    other = TestClient(create_app(service_engine), base_url="http://evil.example")

    reply = other.get("/health")

    assert reply.json()["code"] == "wrong_host"
    assert reply.headers["x-frame-options"] == "DENY"


@pytest.mark.parametrize(
    "content",
    [
        "[" * 50_000,
        '{"code": ' + "[" * 20_000,
        '{"code": "\\ud800", "minutes": 5}',
        '{"code": "I3302", "minutes": 5}' + " " * 100_001,
    ],
    ids=["deep", "deep in a field", "half an emoji", "too long"],
)
def test_a_hostile_body_is_refused_not_a_crash(client, service_engine, web, content):
    reply = client.post("/hours", content=content, headers={"Content-Type": "application/json"})

    assert reply.status_code == 400
    assert reply.json()["code"] == "invalid_value"
    assert entries(service_engine) == 0
