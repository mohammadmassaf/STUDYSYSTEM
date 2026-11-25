"""The dashboard's lock (D-369): the secret, the one-time link, and what answers without a key."""

import io
import json
import os
import sys
import time

import pytest
from click.testing import CliRunner
from starlette.testclient import TestClient

from studysystem import dashboard
from studysystem.cli import study
from studysystem.dashboard import create_app
from studysystem.db.tables import generation_directive
from studysystem.errors import StudyError
from studysystem.services import dashboard_key, launcher
from tests.test_active_directives import directive
from tests.test_dashboard import OWN, web  # noqa: F401 - a fixture

# every route that reads or writes the student's data
DATA = [
    ("GET", "/state"),
    ("POST", "/hours"),
    ("POST", "/sessions"),
    ("POST", "/sessions/stop"),
    ("POST", "/capacity"),
    ("POST", "/capacity/accept"),
    ("POST", "/directives/confirm"),
    ("POST", "/trials"),
]


@pytest.fixture
def stops():
    return []


@pytest.fixture
def client(service_engine, user_id, stops):
    app = create_app(service_engine, stop=lambda: stops.append(1), secret=dashboard_key.key())
    return TestClient(app, base_url=OWN)


def enter(client) -> str:
    reply = client.post("/open", json={"code": dashboard_key.code()})
    assert reply.status_code == 200
    return reply.json()["key"]


def test_the_list_above_is_every_data_route(client):
    routes = {(m, r.path) for r in client.app.routes for m in r.methods if m != "HEAD"}

    assert routes - set(DATA) == {
        ("GET", "/"),
        ("GET", "/health"),
        ("POST", "/open"),
        ("POST", "/shutdown"),
    }


@pytest.mark.parametrize(("method", "path"), [*DATA, ("POST", "/shutdown"), ("GET", "/nowhere")])
@pytest.mark.parametrize("key", [None, "", "x" * 64, "\u00e9"])
def test_nothing_answers_without_the_key(client, stops, method, path, key):
    headers = {} if key is None else {dashboard_key.HEADER: key.encode("latin-1")}

    reply = client.request(method, path, headers=headers, json={} if method == "POST" else None)

    assert (reply.status_code, reply.json()["code"]) == (401, "dashboard_locked")
    assert stops == []


def test_the_page_and_health_answer_and_hold_no_data(client, service_engine, web):  # noqa: F811
    assert client.get("/").status_code == 200
    assert set(client.get("/health").json()) == {"server_version", "schema_revision"}
    assert "Server-Side Web" not in client.get("/").text


def test_the_link_gives_the_browser_a_key_that_works(client, service_engine, web):  # noqa: F811
    client.headers[dashboard_key.HEADER] = enter(client)

    assert client.get("/state").status_code == 200
    assert client.post("/hours", json={"code": "I3302", "minutes": 5}).status_code == 200


def test_a_code_works_once(client):
    code = dashboard_key.code()

    assert client.post("/open", json={"code": code}).status_code == 200
    assert client.post("/open", json={"code": code}).json()["code"] == "dashboard_locked"


def test_a_code_ends(client, monkeypatch):
    code = dashboard_key.code()
    now = time.time()
    monkeypatch.setattr(dashboard_key.time, "time", lambda: now + dashboard_key.CODE_SECONDS + 1)

    assert client.post("/open", json={"code": code}).status_code == 401


@pytest.mark.parametrize(
    "code", [None, 5, "", "a.b.c", "9999999999.n.x", "..", "\u00e9.\u00e9.\u00e9", "1.2.3.4"]
)
def test_a_code_that_is_not_ours_is_refused(client, code):
    assert client.post("/open", json={"code": code}).status_code == 401


def test_a_code_cannot_be_stretched(client):
    ends, nonce, mark = dashboard_key.code().split(".")

    assert (
        client.post("/open", json={"code": f"{int(ends) + 999}.{nonce}.{mark}"}).status_code == 401
    )


def test_the_browser_key_cannot_stop_it_and_the_stop_key_reads_nothing(client, stops):
    browser, stop = enter(client), dashboard_key.token("stop")

    assert (
        client.post("/shutdown", json={}, headers={dashboard_key.HEADER: browser}).status_code
        == 401
    )
    assert client.get("/state", headers={dashboard_key.HEADER: stop}).status_code == 401
    assert (
        client.post("/shutdown", json={}, headers={dashboard_key.HEADER: stop}).status_code == 200
    )
    assert stops == [1]


def test_a_proof_is_no_key(client):
    """/health answers any nonce to anyone: what it gives must open nothing."""
    for nonce in ("browser", "token:browser", "stop", "code:9999999999.n"):
        proof = client.get("/health", params={"nonce": nonce}).json()["proof"]
        assert client.get("/state", headers={dashboard_key.HEADER: proof}).status_code == 401
    fake = client.get("/health", params={"nonce": "code:9999999999.n"}).json()["proof"]
    assert client.post("/open", json={"code": f"9999999999.n.{fake}"}).status_code == 401


def test_a_long_nonce_gets_no_proof(client):
    assert "proof" not in client.get("/health", params={"nonce": "n" * 65}).json()


def test_another_site_cannot_trade_a_code(client):
    evil = TestClient(client.app, base_url="http://evil.example:8765")

    assert evil.post("/open", json={"code": dashboard_key.code()}).json()["code"] == "wrong_host"
    plain = json.dumps({"code": dashboard_key.code()})
    assert client.post("/open", content=plain).json()["code"] == "not_json"


def test_an_injected_host_cannot_confirm_through_the_port(client, service_engine, user_id):
    """Findings 3 and 6 together: the tool is gone, and the port needs the key."""
    made = directive(service_engine, user_id, "embed a link", status="proposed")

    reply = client.post("/directives/confirm", json={"directive_id": made})

    assert reply.status_code == 401
    with service_engine.connect() as conn:
        assert conn.execute(generation_directive.select()).one().status == "proposed"


def test_the_secret_is_made_once_and_kept(isolated_data_dir):
    first = dashboard_key.key()

    assert dashboard_key.key() == first and len(first) == 32
    if sys.platform != "win32":
        assert os.stat(isolated_data_dir / dashboard_key.FILE).st_mode & 0o077 == 0


def test_another_data_folder_has_another_secret(client, monkeypatch, tmp_path):
    mine = enter(client)
    monkeypatch.setenv("STUDYSYSTEM_DATA_DIR", str(tmp_path / "other"))

    assert dashboard_key.token("browser") != mine
    assert client.post("/open", json={"code": dashboard_key.code()}).status_code == 401


def test_the_launcher_knows_its_own_dashboard_from_another(client, monkeypatch, tmp_path):
    class Reply:
        def __init__(self, data):
            self.data = data

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return None

        def read(self, *a):
            return self.data

    def through(address, timeout):
        return Reply(client.get(address.replace("http://127.0.0.1:8765", "")).content)

    monkeypatch.setattr(launcher._DIRECT, "open", through)

    assert launcher._whose(8765) == "ours"
    monkeypatch.setenv("STUDYSYSTEM_DATA_DIR", str(tmp_path / "other"))
    assert launcher._whose(8765) == "another"


def test_the_demo_asks_no_key(service_engine, user_id):
    demo = TestClient(create_app(service_engine), base_url=OWN)

    assert demo.get("/state").status_code == 200
    assert demo.post("/open", json={"code": "x"}).status_code == 404


@pytest.mark.parametrize(
    ("body", "whose"),
    [
        (b'{"server_version": "1.0.0rc1", "schema_revision": null}', "unlocked"),
        (b'{"server_version": "1", "proof": "0000"}', "another"),
        (b'{"server_version": "1", "proof": 5}', "another"),
        (b"[]", "another"),
        (b"<html>", "another"),
        (None, "silent"),
    ],
)
def test_what_answers_there_is_told_apart(monkeypatch, body, whose):
    def answers(address, timeout):
        if body is None:
            raise TimeoutError
        return io.BytesIO(body)

    monkeypatch.setattr(launcher._DIRECT, "open", answers)

    assert launcher._whose(8765) == whose


@pytest.mark.parametrize("held", [b"", b"12ab", b"z" * 64, bytes([255]) * 64])
def test_a_damaged_key_file_is_a_structured_refusal(isolated_data_dir, monkeypatch, held):
    isolated_data_dir.mkdir(exist_ok=True)
    (isolated_data_dir / dashboard_key.FILE).write_bytes(held)
    monkeypatch.setattr(dashboard_key.time, "sleep", lambda s: None)

    with pytest.raises(StudyError) as caught:
        dashboard_key.key()

    assert caught.value.code == "dashboard_key_unreadable"
    assert dashboard_key.FILE in caught.value.message


def test_study_dashboard_says_a_damaged_key_in_one_line(isolated_data_dir, monkeypatch):
    isolated_data_dir.mkdir(exist_ok=True)
    (isolated_data_dir / dashboard_key.FILE).write_text("damaged")
    monkeypatch.setattr(dashboard_key.time, "sleep", lambda s: None)
    monkeypatch.setattr(dashboard, "open_database", lambda: None)
    monkeypatch.setattr(dashboard.uvicorn, "run", lambda *a, **k: pytest.fail("served"))

    out = CliRunner().invoke(study, ["dashboard"])

    assert out.exit_code == 1
    assert "dashboard_key_unreadable" in out.output


def test_the_demo_has_no_shutdown(monkeypatch):
    """It asks no key, so any program could stop it."""
    apps = []
    monkeypatch.setattr(dashboard, "open_database", lambda: None)
    monkeypatch.setattr(dashboard.uvicorn, "run", lambda app, **k: apps.append(app))

    dashboard.main(8766, locked=False)

    assert "/shutdown" not in {r.path for r in apps[0].routes}
