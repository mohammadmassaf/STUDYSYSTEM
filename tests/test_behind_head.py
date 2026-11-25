"""A database the build did not expect (5.4; D-325, D-330, D-338): both front doors start behind
head, every tool but `study_dashboard` refuses, every data route refuses, and no row changes.
The check runs on every call, so `study migrate` needs no restart. The databases here are migrated,
not made by `create_all`: the check reads the revision only a migration writes."""

import asyncio
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from alembic import command
from mcp import Client, StdioServerParameters
from sqlalchemy import insert, inspect, text
from sqlalchemy.exc import OperationalError
from starlette.testclient import TestClient

from studysystem import dashboard, startup
from studysystem import server as mcp_server
from studysystem.dashboard import create_app
from studysystem.db import migrate
from studysystem.db.engine import (
    HOW_TO_RESTORE,
    data_dir,
    db_path,
    make_engine,
    unavailable,
)
from studysystem.db.tables import course, hours_entry, user
from studysystem.errors import StudyError
from studysystem.server import create_server
from studysystem.services import dashboard_key, launcher
from studysystem.services.ids import new_id
from studysystem.services.users import current_user
from studysystem.startup import call_check, open_database
from studysystem.tools.results import refusal
from tests._client import call_tool, tool_names
from tests._study import run_study

HEAD = migrate.head_revision()
BEHIND = "0025"  # pinned, for the rows below; 0026 adds user.trials_on
OWN = "http://127.0.0.1:8765"
AT = "2026-11-21T13:00:00Z"
REFUSED = {
    "code": "schema_behind_head",
    "message": f"database is at {BEHIND}, head is {HEAD}",
    "field_errors": [],
    "fix": "run: study migrate",
}


def _behind(engine) -> None:
    """His I3302 with 40 logged minutes, in a database one revision behind."""
    command.upgrade(migrate.alembic_config(engine), BEHIND)
    uid, cid = new_id(), new_id()
    with engine.begin() as conn:
        conn.execute(insert(user).values(id=uid, created_at=AT))
        conn.execute(
            insert(course).values(
                id=cid,
                user_id=uid,
                code="I3302",
                name="Server-Side Web Development",
                semester_name="Semester 1 2026-2027",
                instructor_tier="unknown",
                credits_tier="unknown",
                created_at=AT,
            )
        )
        conn.execute(
            insert(hours_entry).values(
                id=new_id(),
                user_id=uid,
                course_id=cid,
                minutes=40,
                source="tool",
                occurred_at=AT,
                created_at=AT,
            )
        )


def _rows(engine) -> dict[str, list]:
    """Every row of every table, as the database itself holds them: not the code's tables, which
    have a column this database lacks."""
    with engine.connect() as conn:
        return {
            name: sorted(map(repr, conn.execute(text(f'SELECT * FROM "{name}"')).all()))
            for name in inspect(conn).get_table_names()
        }


@pytest.fixture
def behind(empty_engine):
    _behind(empty_engine)
    return empty_engine


# --- the tools ---------------------------------------------------------------------------------


def test_every_tool_but_study_dashboard_refuses_and_no_row_changes(behind, monkeypatch):
    monkeypatch.setattr(launcher, "dashboard", lambda action: {"url": OWN})
    before = _rows(behind)
    server = create_server(behind, migrate.schema_check(behind))
    names = tool_names(server)

    results = {name: call_tool(server, name, {}) for name in names}

    ran = {name for name, r in results.items() if not r.is_error}
    assert ran == {"study_dashboard"}
    for name in names - ran:
        assert results[name].structured_content == REFUSED, name
        assert json.loads(results[name].content[0].text) == REFUSED, name
    assert _rows(behind) == before
    assert before["hours_entry"]  # the comparison had a row to lose


def test_a_call_that_would_write_is_refused_before_it_writes(behind):
    """`log_hours` never reads what 0026 added, so without the check this call writes a row."""
    server = create_server(behind, migrate.schema_check(behind))
    before = _rows(behind)
    args = {"code": "I3302", "minutes": 30}

    result = call_tool(server, "study_log_hours", args)

    assert result.structured_content == REFUSED
    assert _rows(behind) == before

    unchecked = call_tool(create_server(behind), "study_log_hours", args)
    assert not unchecked.is_error
    assert len(_rows(behind)["hours_entry"]) == len(before["hours_entry"]) + 1


def test_the_next_call_after_a_migrate_works_on_the_same_server(behind):
    server = create_server(behind, migrate.schema_check(behind))
    assert call_tool(server, "study_ping", {}).is_error

    migrate.upgrade(behind)

    assert not call_tool(server, "study_ping", {}).is_error
    logged = call_tool(server, "study_log_hours", {"code": "I3302", "minutes": 30})
    assert not logged.is_error


def test_a_database_with_no_revision_is_behind_too(empty_engine):
    server = create_server(empty_engine, migrate.schema_check(empty_engine))

    result = call_tool(server, "study_get_plan", {})

    assert result.structured_content["code"] == "schema_behind_head"
    assert "has no schema yet" in result.structured_content["message"]


# --- a newer build under a running server ------------------------------------------------------


@pytest.fixture
def newer_build(monkeypatch, tmp_path):
    """Call it to put a build with one more migration (9999) where this one's files are, as an
    upgrade does while the old server still runs."""
    folder = tmp_path / "migrations"
    shutil.copytree(migrate.MIGRATIONS_DIR, folder, ignore=shutil.ignore_patterns("__pycache__"))

    def arrive() -> None:
        (folder / "versions" / "9999_newer.py").write_text(
            f'revision = "9999"\ndown_revision = "{HEAD}"\nbranch_labels = None\n'
            "depends_on = None\n\n\ndef upgrade():\n    pass\n\n\ndef downgrade():\n    pass\n"
        )

    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", folder)
    return arrive


def test_a_database_a_newer_build_migrated_refuses_the_old_running_server(
    empty_engine, newer_build
):
    """Head read from disk would pass here, and head alone remembered would say `study migrate`."""
    migrate.upgrade(empty_engine)
    check = migrate.schema_check(empty_engine)
    server, page = create_server(empty_engine, check), create_app(empty_engine, check)
    assert not call_tool(server, "study_ping", {}).is_error

    newer_build()
    migrate.upgrade(empty_engine)

    tool = call_tool(server, "study_ping", {}).structured_content
    route = TestClient(page, base_url=OWN).get("/state")
    assert tool["code"] == "schema_ahead_of_head"
    assert tool["message"] == f"database is at 9999, unknown to this build (its head is {HEAD})"
    assert "restart" in tool["fix"]
    assert tool["fix"].endswith("run: study stop, then: study start")  # the dashboard (D-342)
    assert "snapshot" not in tool["fix"]  # restoring one would lose the newer data
    assert "study migrate" not in tool["fix"]
    assert (route.status_code, route.json()) == (503, tool)


def test_the_start_still_offers_a_snapshot_ahead_of_head(empty_engine, newer_build):
    """At start, this build is the old one and stays the old one: a restart cannot help."""
    newer_build()
    migrate.upgrade(empty_engine)
    (migrate.MIGRATIONS_DIR / "versions" / "9999_newer.py").unlink()

    with pytest.raises(migrate.SchemaAheadOfHead) as e:
        migrate.check_schema(empty_engine)
    assert "snapshot" in e.value.fix
    assert "restart" not in e.value.fix


# --- the check itself --------------------------------------------------------------------------


def test_the_check_reads_the_migration_files_once(empty_engine, monkeypatch):
    migrate.upgrade(empty_engine)
    check = migrate.schema_check(empty_engine)
    monkeypatch.setattr(migrate, "MIGRATIONS_DIR", Path("gone"))

    check()
    check()


def _locked():
    raise OperationalError("SELECT ...", {}, sqlite3.OperationalError("database is locked"))


def test_a_locked_database_during_the_check_is_database_busy(empty_engine):
    server = create_server(empty_engine, _locked)
    page = TestClient(create_app(empty_engine, _locked), base_url=OWN)

    assert call_tool(server, "study_ping", {}).structured_content["code"] == "database_busy"
    assert page.get("/state").status_code == 503
    assert page.get("/state").json()["code"] == "database_busy"


def test_any_other_database_error_in_the_check_is_a_crash_and_the_tool_does_not_run(empty_engine):
    def broken():
        raise OperationalError("SELECT ...", {}, sqlite3.OperationalError("disk I/O error"))

    with pytest.raises(OperationalError):
        refusal(broken)
    result = call_tool(create_server(empty_engine, broken), "study_ping", {})
    assert result.is_error
    assert result.structured_content is None


def test_refusal_is_none_at_head():
    assert refusal(lambda: None) is None


# --- the dashboard -----------------------------------------------------------------------------


def _data_routes(app) -> list[tuple[str, str]]:
    return [
        (method, route.path)
        for route in app.routes
        for method in route.methods - {"HEAD"}
        if route.path not in ("/", "/health")
    ]


def test_every_data_route_refuses_and_no_row_changes(behind):
    before = _rows(behind)
    app = create_app(behind, migrate.schema_check(behind))
    client = TestClient(app, base_url=OWN)
    routes = _data_routes(app)
    assert ("GET", "/state") in routes and ("POST", "/hours") in routes

    for method, path in routes:
        body = {"json": {"code": "I3302", "minutes": 30}} if method == "POST" else {}
        reply = client.request(method, path, **body)
        assert (reply.status_code, reply.json()) == (503, REFUSED), path

    assert _rows(behind) == before


def test_the_page_and_health_answer_behind_head(behind):
    client = TestClient(create_app(behind, migrate.schema_check(behind)), base_url=OWN)

    assert client.get("/").status_code == 200
    health = client.get("/health")
    assert health.status_code == 200  # launcher.answering() needs it
    assert health.json()["schema_revision"] == BEHIND


def test_another_host_is_refused_before_the_schema_is_named(behind):
    client = TestClient(
        create_app(behind, migrate.schema_check(behind)), base_url="http://evil.example:8765"
    )

    assert client.get("/state").json()["code"] == "wrong_host"


def test_a_route_works_after_a_migrate_on_the_same_dashboard(behind):
    client = TestClient(create_app(behind, migrate.schema_check(behind)), base_url=OWN)
    assert client.get("/state").status_code == 503

    migrate.upgrade(behind)

    assert client.get("/state").status_code == 200
    assert client.post("/hours", json={"code": "I3302", "minutes": 30}).status_code == 200


def test_the_page_keeps_the_servers_reason_for_a_failed_state_request():
    """What the page draws was seen in a browser (5.4). This keeps the code that reads the body:
    `load` throws the server's own error, and the first load prints its message and its fix."""
    page = dashboard.PAGE.read_text(encoding="utf-8")

    load = page[page.index("async function load()") : page.index("\nnav();")]
    failed = load[load.index("if(!r.ok)") : load.index("adapt(")]
    assert "r.json()" in failed
    assert "The state request failed" in failed  # a body that is not our JSON
    first = page[page.index(".then(load).catch") :]
    assert "e.code" in first and "e.message" in first and "e.fix" in first


# --- a database lost while the server runs (D-339) ---------------------------------------------


@pytest.fixture
def running():
    """A server's engine and check on a ready database file, with one connection open."""
    engine = open_database()
    check = call_check(engine)
    check()
    yield engine, check
    engine.dispose()


def _lose(engine) -> None:
    engine.dispose()  # Windows deletes no file that is open
    for end in ("", "-wal", "-shm"):
        Path(f"{db_path()}{end}").unlink(missing_ok=True)


def _empty_file(engine) -> None:
    _lose(engine)
    sqlite3.connect(db_path()).close()


@pytest.mark.parametrize("lose", [_lose, _empty_file])
def test_a_lost_database_is_database_missing_and_not_study_migrate_alone(running, lose):
    engine, check = running
    lose(engine)

    tool = call_tool(create_server(engine, check), "study_log_hours", {"code": "I3302"})
    route = TestClient(create_app(engine, check), base_url=OWN).get("/state")

    body = tool.structured_content
    assert body["code"] == "database_missing"
    assert str(db_path()) in body["message"]
    assert "snapshots" in body["fix"] and "study migrate" in body["fix"]  # both ways out
    assert (route.status_code, route.json()) == (503, body)


def test_a_missing_file_is_refused_before_it_is_opened(running):
    engine, check = running
    _lose(engine)

    with pytest.raises(StudyError):
        check()

    assert not db_path().exists()


def test_no_snapshot_is_needed_to_call_a_running_database_lost(running):
    engine, check = running
    shutil.rmtree(data_dir() / "snapshots")
    _lose(engine)

    with pytest.raises(StudyError) as e:
        check()
    assert e.value.code == "database_missing"


def test_the_next_call_after_the_file_is_put_back_works(running):
    engine, check = running
    kept = next((data_dir() / "snapshots").glob("*.db"))
    _lose(engine)
    with pytest.raises(StudyError):
        check()

    shutil.copy(kept, db_path())

    check()


def test_study_migrate_starts_over_under_the_running_server(running):
    engine, check = running
    _lose(engine)
    with pytest.raises(StudyError):
        check()

    assert run_study("migrate").returncode == 0

    check()


def test_a_lost_file_closes_the_open_connections(running, monkeypatch, tmp_path):
    """On a Mac a program can keep a deleted file open: a kept connection would write the next
    hours into it, and would still be used once the file was put back."""
    engine, _ = running
    monkeypatch.setattr(startup, "db_path", lambda: tmp_path / "gone.db")
    check = call_check(engine)
    assert engine.pool.checkedin() == 1

    with pytest.raises(StudyError):
        check()

    assert engine.pool.checkedin() == 0


def test_a_database_taken_down_to_base_is_behind_not_lost(running):
    """It still holds Alembic's own table, as at start."""
    engine, check = running
    command.downgrade(migrate.alembic_config(engine), "base")

    with pytest.raises(migrate.SchemaBehindHead):
        check()


def _health(client) -> tuple[int, object]:
    reply = client.get("/health")
    return reply.status_code, reply.json()["schema_revision"]


def test_health_on_a_lost_file_answers_and_makes_no_file(running):
    engine, check = running
    client = TestClient(create_app(engine, check), base_url=OWN)
    _lose(engine)

    assert _health(client) == (200, None)  # unknown, and the launcher still sees a dashboard
    assert not db_path().exists()
    assert client.get("/state").json()["code"] == "database_missing"


def test_health_on_a_lost_folder_answers(running):
    engine, check = running
    client = TestClient(create_app(engine, check), base_url=OWN)
    engine.dispose()
    shutil.rmtree(data_dir())

    assert _health(client) == (200, None)
    assert client.get("/state").json()["code"] == "database_missing"


def _denied(*_args, **_kw):
    raise PermissionError(13, "Permission denied")


def test_an_unreadable_folder_is_unavailable_not_missing(running, monkeypatch):
    """Unknown is not missing: `study migrate` must not be offered as a way to start over for a
    file that may be there."""
    engine, check = running
    client = TestClient(create_app(engine, check), base_url=OWN)
    monkeypatch.setattr(startup, "os", SimpleNamespace(stat=_denied))

    with pytest.raises(StudyError) as e:
        check()
    assert e.value.code == "database_unavailable"
    assert "permissions" in e.value.fix
    reply = client.get("/state")
    assert (reply.status_code, reply.json()["code"]) == (503, "database_unavailable")
    assert _health(client) == (200, None)


def test_a_start_on_an_unreadable_folder_is_one_line(monkeypatch, capsys):
    """`has_snapshot` already answers yes there, so the start looks for the file itself."""
    path = db_path()
    monkeypatch.setattr(startup, "has_snapshot", lambda: True)
    monkeypatch.setattr(startup, "db_path", lambda: path)
    monkeypatch.setattr(startup, "os", SimpleNamespace(stat=_denied))

    with pytest.raises(SystemExit) as e:
        open_database()

    assert e.value.code == 1
    assert capsys.readouterr().err.startswith("database_unavailable: ")


# --- restoring a copy (D-340) ------------------------------------------------------------------


def _crash_with_a_wal() -> None:
    """Another program commits one user's row, then dies: the row is in the -wal file only."""
    sql = f"INSERT INTO user (id, created_at) VALUES ('{new_id()}', '{AT}')"
    code = (
        "import os, sqlite3, sys; c = sqlite3.connect(sys.argv[1]); c.execute(sys.argv[2]); "
        "c.commit(); os._exit(1)"
    )
    subprocess.run([sys.executable, "-c", code, str(db_path()), sql])
    assert Path(f"{db_path()}-wal").stat().st_size > 0


def _integrity() -> str:
    db = sqlite3.connect(db_path())
    try:
        return db.execute("PRAGMA integrity_check").fetchone()[0]
    except sqlite3.DatabaseError as e:
        return str(e)
    finally:
        db.close()


@pytest.fixture
def crashed():
    """A day's copy with one user, then a crash that leaves a second user in the -wal file."""
    open_database().dispose()
    copy = next((data_dir() / "snapshots").glob("*.db"))
    _crash_with_a_wal()
    return copy


def test_a_copy_restored_as_the_fix_says_is_the_copy(crashed, tmp_path):
    for end in ("", "-wal", "-shm"):
        shutil.move(f"{db_path()}{end}", tmp_path)
    shutil.copy(crashed, db_path())

    assert _integrity() == "ok"
    assert len(_file_rows()["user"]) == 1  # the crash's row is not in the copy


def test_a_copy_put_over_a_left_over_wal_is_not_the_copy(crashed):
    """Why the fix says to move them: SQLite writes the old file's pages over the restored one.
    The result is damaged, or quietly holds the crash's rows: which one depends on the pages."""
    db_path().unlink()
    shutil.copy(crashed, db_path())

    assert _integrity() != "ok" or len(_file_rows()["user"]) != 1


def test_every_fix_that_says_restore_says_how():
    engine = open_database()
    with engine.begin() as conn:
        conn.execute(insert(user).values(id=new_id(), created_at=AT, trials_on=0))
    with pytest.raises(StudyError) as two_users:
        current_user(engine)
    engine.dispose()
    fixes = [
        two_users.value.fix,
        startup._missing(db_path(), startup.HAD_COPIES).fix,
        unavailable(db_path(), OSError("damaged")).fix,
        migrate.SchemaAheadOfHead("9999", HEAD).fix,
    ]
    for fix in fixes:
        assert HOW_TO_RESTORE in fix
    assert "delete" not in HOW_TO_RESTORE  # a healthy file's -wal can hold the newest hours
    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
    assert "studysystem.db-wal" in readme and "studysystem.db-shm" in readme


def test_both_front_doors_run_the_lost_file_check(monkeypatch):
    checks = []
    served = []
    monkeypatch.setattr(mcp_server, "create_server", lambda engine, check: served.append(check))
    monkeypatch.setattr(
        dashboard, "create_app", lambda engine, check, _stop, _secret: served.append(check)
    )
    monkeypatch.setattr(dashboard.uvicorn, "run", lambda app, **_kw: None)
    monkeypatch.setattr(mcp_server, "call_check", lambda engine: checks.append("server") or "s")
    monkeypatch.setattr(dashboard, "call_check", lambda engine: checks.append("page") or "p")

    with pytest.raises(AttributeError):  # the stub server has no run()
        mcp_server.main()
    dashboard.main(8765)

    assert (checks, served) == (["server", "page"], ["s", "p"])


# --- the start ---------------------------------------------------------------------------------


def _file_behind() -> None:
    engine = make_engine(db_path())
    _behind(engine)
    engine.dispose()


def _file_rows() -> dict[str, list]:
    engine = make_engine(db_path())
    try:
        return _rows(engine)
    finally:
        engine.dispose()


def test_a_start_behind_head_writes_nothing_and_takes_no_snapshot(caplog):
    _file_behind()
    before = _file_rows()

    open_database().dispose()

    assert _file_rows() == before
    assert not (data_dir() / "snapshots").exists()
    assert "study migrate" in caplog.text  # the host's log still names it


def test_a_start_behind_head_makes_no_user():
    engine = make_engine(db_path())
    command.upgrade(migrate.alembic_config(engine), BEHIND)
    engine.dispose()

    open_database().dispose()

    assert _file_rows()["user"] == []


def test_a_start_ahead_of_head_is_still_refused(monkeypatch, capsys):
    """Only `SchemaBehindHead` lets the start go on."""
    _file_behind()

    def ahead(_engine):
        raise migrate.SchemaAheadOfHead("9999", HEAD)

    monkeypatch.setattr(startup, "check_schema", ahead)

    with pytest.raises(SystemExit) as e:
        open_database()
    assert e.value.code == 1
    assert "schema_ahead_of_head" in capsys.readouterr().err


def test_serve_behind_head_refuses_then_works_after_study_migrate():
    """The way Claude Desktop starts it, and the way the student fixes it: one server, one
    connection, `study migrate` run beside it."""
    _file_behind()
    before = _file_rows()
    study = shutil.which("study", path=str(Path(sys.executable).parent))
    assert study is not None
    server = StdioServerParameters(command=study, args=["serve"], env=dict(os.environ))
    log = {"code": "I3302", "minutes": 30}

    async def go():
        async with Client(server, read_timeout_seconds=60) as client:
            read = await client.call_tool("study_get_plan", {})
            write = await client.call_tool("study_log_hours", log)
            between = _file_rows()
            ran = await asyncio.to_thread(run_study, "migrate")
            after = await client.call_tool("study_log_hours", log)
            return read, write, between, ran, after

    read, write, between, ran, after = asyncio.run(go())

    assert read.structured_content == REFUSED
    assert write.structured_content == REFUSED
    assert between == before
    assert ran.returncode == 0, ran.stderr
    assert not after.is_error
    assert len(_file_rows()["hours_entry"]) == len(before["hours_entry"]) + 1


def test_study_dashboard_behind_head_serves_a_checked_app(monkeypatch):
    _file_behind()
    served = []
    monkeypatch.setattr(dashboard.uvicorn, "run", lambda app, **_kw: served.append(app))

    dashboard.main(8765)

    headers = {dashboard_key.HEADER: dashboard_key.token("browser")}
    reply = TestClient(served[0], base_url=OWN, headers=headers).get("/state")
    assert (reply.status_code, reply.json()) == (503, REFUSED)
    assert TestClient(served[0], base_url=OWN).get("/state").status_code == 401  # it is locked
