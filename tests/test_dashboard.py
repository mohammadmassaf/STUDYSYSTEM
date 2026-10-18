"""The dashboard route (3.1; D-17, D-161, D-162): the same services the tools call, behind HTTP.
Each write is his 40 minutes on I3302, sent as the dashboard would send it."""

import datetime
import importlib.metadata
import sqlite3

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import OperationalError
from starlette.testclient import TestClient

from studysystem.dashboard import create_app
from studysystem.db.engine import db_path
from studysystem.db.migrate import head_revision
from studysystem.db.tables import hours_entry, study_session
from studysystem.services import study_loop
from studysystem.services.courses import add_course
from tests._study import run_study

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


@pytest.mark.parametrize("path", ["/hours", "/sessions", "/sessions/stop"])
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


# --- `study dashboard`, started the way he starts it ------------------------------------------


def test_study_dashboard_refuses_an_empty_database_as_serve_does():
    """The startup refusals are serve's own (startup.open_database); one case proves they run."""
    proc = run_study("dashboard")

    assert proc.returncode == 1
    assert f"database has no schema yet, head is {head_revision()}" in proc.stderr
    assert "run: study migrate" in proc.stderr
    assert not db_path().exists()
