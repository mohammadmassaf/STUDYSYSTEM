"""The first start on a machine (5.3; D-324, D-335): `study serve` and `study dashboard` build a
brand-new database and its one user. A database that already has tables is never migrated here."""

import asyncio
import datetime
import os
import shutil
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from alembic import command
from mcp import Client, StdioServerParameters
from sqlalchemy import event, insert

from studysystem import dashboard, startup
from studysystem.db import migrate
from studysystem.db.engine import data_dir, db_path, make_engine, snapshot
from studysystem.db.tables import user
from studysystem.services.ids import new_id, now
from studysystem.services.users import ensure_user
from studysystem.startup import open_database
from tests._study import run_study


def study_script() -> str:
    script = shutil.which("study", path=str(Path(sys.executable).parent))
    assert script is not None
    return script


def revision_and_users() -> tuple[str, int]:
    """What the file holds, read with plain sqlite3."""
    with sqlite3.connect(db_path()) as db:
        revision = db.execute("SELECT version_num FROM alembic_version").fetchone()[0]
        return revision, db.execute("SELECT count(*) FROM user").fetchone()[0]


def test_serve_on_an_empty_data_dir_answers_ping_with_the_database_at_head():
    """The way Claude Desktop starts it. The reply also proves the build printed nothing on
    stdout, which carries the MCP messages."""
    server = StdioServerParameters(command=study_script(), args=["serve"], env=dict(os.environ))

    async def ping():
        async with Client(server, read_timeout_seconds=30) as client:
            return await client.call_tool("study_ping", {})

    result = asyncio.run(ping())

    assert not result.is_error
    assert revision_and_users() == (migrate.head_revision(), 1)
    assert not list((data_dir() / "snapshots").glob("*premigrate*"))  # nothing to keep a copy of


def test_the_dashboard_builds_a_missing_database_too(monkeypatch):
    served = []
    monkeypatch.setattr(dashboard.uvicorn, "run", lambda app, **_kw: served.append(app))

    dashboard.main(8765)

    assert len(served) == 1
    assert revision_and_users() == (migrate.head_revision(), 1)


def test_two_first_starts_at_once_both_start_and_make_one_user():
    """Claude Desktop and Cowork each start a server on a new machine."""
    starts = [
        subprocess.Popen(
            [study_script(), "serve"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            text=True,
        )
        for _ in range(2)
    ]
    errors = [p.communicate(timeout=60)[1] for p in starts]

    assert [p.returncode for p in starts] == [0, 0], errors
    assert revision_and_users() == (migrate.head_revision(), 1)


def test_a_start_killed_before_its_tables_were_saved_is_built_by_the_next():
    """A killed build leaves the file it opened, with no tables in it."""
    engine = make_engine(db_path())
    engine.connect().close()
    engine.dispose()
    assert db_path().exists()

    assert run_study("serve").returncode == 0

    assert revision_and_users() == (migrate.head_revision(), 1)


def test_a_start_killed_before_its_user_was_made_is_finished_by_the_next():
    migrate.upgrade(make_engine(db_path()))
    assert revision_and_users() == (migrate.head_revision(), 0)

    assert run_study("serve").returncode == 0

    assert revision_and_users() == (migrate.head_revision(), 1)


def test_a_database_taken_down_to_base_is_not_rebuilt():
    """It still holds Alembic's own table, so it is an existing database: behind head. The server
    starts, and its tools refuse (D-338)."""
    engine = make_engine(db_path())
    migrate.upgrade(engine)
    command.downgrade(migrate.alembic_config(engine), "base")
    engine.dispose()

    proc = run_study("serve")

    assert proc.returncode == 0  # stdin closed: served, then ended
    assert "run: study migrate" in proc.stderr
    with sqlite3.connect(db_path()) as db:
        tables = db.execute("SELECT name FROM sqlite_master WHERE type = 'table'").fetchall()
    assert tables == [("alembic_version",)]


def test_a_start_on_a_ready_database_takes_no_write_lock():
    """A server starts while the dashboard is in the middle of a write: it only reads, so it does
    not wait."""
    ready = make_engine(db_path())
    migrate.upgrade(ready)
    ensure_user(ready)
    ready.dispose()
    holder = sqlite3.connect(db_path(), isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        engine = open_database()
    finally:
        holder.close()

    engine.dispose()
    assert revision_and_users() == (migrate.head_revision(), 1)


def test_two_users_still_start_and_are_left_alone():
    """`too_many_users` is each tool's error (D-31); a start neither hides it nor changes it."""
    engine = make_engine(db_path())
    migrate.upgrade(engine)
    ensure_user(engine)
    with engine.begin() as conn:
        conn.execute(insert(user).values(id=new_id(), created_at=now()))
    engine.dispose()

    open_database().dispose()

    assert revision_and_users() == (migrate.head_revision(), 2)


@pytest.mark.parametrize("built", [False, True])
def test_has_tables(empty_engine, built):
    if built:
        migrate.upgrade(empty_engine)
    assert migrate.has_tables(empty_engine) is built


# --- a snapshot means this machine already had a database (D-337) -------------------------------


def _used_then(lose) -> None:
    """A machine whose first start is done, so the day's copy exists; then `lose` the data."""
    open_database().dispose()
    assert list((data_dir() / "snapshots").glob("*.db"))
    for file in data_dir().glob("studysystem.db*"):
        file.unlink()
    lose()


def _no_tables() -> None:
    engine = make_engine(db_path())
    engine.connect().close()
    engine.dispose()


def test_a_deleted_database_is_refused_while_a_copy_exists():
    _used_then(lambda: None)

    proc = run_study("serve")

    assert proc.returncode == 1
    assert proc.stderr.startswith("database_missing: ")
    assert "snapshots" in proc.stderr and "study migrate" in proc.stderr  # both ways out
    assert "Traceback" not in proc.stderr
    assert not db_path().exists()  # refused before opening it: no empty file left behind


def test_an_emptied_database_is_refused_while_a_copy_exists():
    _used_then(_no_tables)

    proc = run_study("dashboard")

    assert proc.returncode == 1
    assert proc.stderr.startswith("database_missing: ")
    with sqlite3.connect(db_path()) as db:
        assert db.execute("SELECT count(*) FROM sqlite_master").fetchone() == (0,)


def test_study_migrate_starts_over_where_serve_refused():
    """The way out the refusal names."""
    _used_then(lambda: None)

    assert run_study("migrate").returncode == 0

    assert run_study("serve").returncode == 0
    assert revision_and_users() == (migrate.head_revision(), 1)


def test_a_half_written_copy_is_not_a_copy():
    """A first start killed during its snapshot leaves a .partial beside the database; if the
    database is then removed, nothing was lost that a copy could bring back."""
    folder = data_dir() / "snapshots"
    folder.mkdir()
    (folder / "2026-11-23.db.4321.partial").touch()

    open_database().dispose()

    assert revision_and_users() == (migrate.head_revision(), 1)


def test_a_copy_taken_while_this_start_looks_does_not_refuse_it(monkeypatch):
    """The other first start saves its tables and takes its copy just after this one saw no
    tables. The copy is the new database's own, so this start goes on."""

    def stale_look(_engine):
        other = make_engine(db_path())
        migrate.upgrade(other)
        ensure_user(other)
        snapshot(other, datetime.date(2026, 11, 23))
        other.dispose()
        return False

    monkeypatch.setattr(startup, "has_tables", stale_look)

    open_database().dispose()

    assert revision_and_users() == (migrate.head_revision(), 1)


def test_a_snapshots_folder_that_cannot_be_read_counts_as_holding_a_copy():
    """Whether this machine had data is unknown then, so an empty database is not built."""
    (data_dir() / "snapshots").touch()  # a file under the folder's name: listing it is an OSError

    proc = run_study("serve")

    assert proc.returncode == 1
    assert proc.stderr.startswith("database_missing: ")
    assert "Traceback" not in proc.stderr
    assert not db_path().exists()


def test_a_snapshots_folder_that_cannot_be_read_does_not_stop_a_ready_database():
    engine = make_engine(db_path())
    migrate.upgrade(engine)
    ensure_user(engine)
    engine.dispose()
    (data_dir() / "snapshots").touch()

    assert run_study("serve").returncode == 0


def test_a_disk_that_fills_during_the_first_build_is_one_line_and_saves_no_tables(
    monkeypatch, capsys
):
    """SQLite's own page limit, set far below what the schema needs, stands in for the full disk."""

    def small_disk(path):
        engine = make_engine(path)

        @event.listens_for(engine, "connect")
        def _limit(dbapi_conn, _record):
            dbapi_conn.execute("PRAGMA max_page_count = 20")

        return engine

    monkeypatch.setattr(startup, "make_engine", small_disk)

    with pytest.raises(SystemExit):
        open_database()

    err = capsys.readouterr().err
    assert err.startswith("database_unavailable: cannot open")
    assert "database or disk is full" in err
    with sqlite3.connect(db_path()) as db:
        assert db.execute("SELECT count(*) FROM sqlite_master").fetchone() == (0,)


def test_the_first_start_writes_no_warnings_to_the_hosts_log():
    proc = run_study("serve")

    assert proc.returncode == 0
    assert "Warning" not in proc.stderr
