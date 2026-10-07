import datetime
import sqlite3
import subprocess
import sys

import pytest
from alembic import command
from click.testing import CliRunner
from sqlalchemy import CheckConstraint, func, insert, inspect, select
from sqlalchemy.exc import IntegrityError

from studysystem.cli import study
from studysystem.db import migrate
from studysystem.db.engine import data_dir, db_path, make_engine, snapshot
from studysystem.db.tables import (
    course,
    generation_task,
    hours_entry,
    metadata,
    study_session,
    submission,
    user,
)

HEAD = "0006"


def stamp() -> str | None:
    """The revision recorded in the database file, read with plain sqlite3."""
    with sqlite3.connect(db_path()) as db:
        tables = db.execute("SELECT name FROM sqlite_master WHERE name = 'alembic_version'")
        if tables.fetchone() is None:
            return None
        row = db.execute("SELECT version_num FROM alembic_version").fetchone()
        return row[0] if row else None


# --- study migrate / downgrade -------------------------------------------


def test_migrate_upgrades_an_empty_database():
    result = CliRunner().invoke(study, ["migrate"])
    assert result.exit_code == 0, result.output
    assert stamp() == HEAD
    assert f"None -> {HEAD}" in result.output


def test_migrate_twice_is_safe():
    runner = CliRunner()
    assert runner.invoke(study, ["migrate"]).exit_code == 0
    result = runner.invoke(study, ["migrate"])
    assert result.exit_code == 0, result.output
    assert stamp() == HEAD


def test_downgrade_base_reverses_it():
    runner = CliRunner()
    assert runner.invoke(study, ["migrate"]).exit_code == 0
    result = runner.invoke(study, ["downgrade", "base"])
    assert result.exit_code == 0, result.output
    assert stamp() is None
    assert f"{HEAD} -> None" in result.output


def test_migrate_snapshots_even_when_daily_snapshot_exists():
    engine = make_engine(db_path())
    today = datetime.datetime.now().astimezone().date()
    assert snapshot(engine, today) is not None  # the server already ran today
    assert CliRunner().invoke(study, ["migrate"]).exit_code == 0
    names = {p.name for p in (data_dir() / "snapshots").iterdir()}
    daily = f"{today.isoformat()}.db"
    assert daily in names
    assert len(names) == 2
    assert (names - {daily}).pop().startswith(f"{today.isoformat()}-premigrate-")


# --- check_schema ---------------------------------------------------------


def test_check_schema_refuses_an_empty_database():
    engine = make_engine(db_path())
    with pytest.raises(migrate.SchemaBehindHead) as excinfo:
        migrate.check_schema(engine)
    err = excinfo.value
    assert err.code == "schema_behind_head"
    assert err.fix == "run: study migrate"
    assert (err.current, err.head) == (None, HEAD)


def test_check_schema_passes_at_head():
    engine = make_engine(db_path())
    migrate.upgrade(engine)
    migrate.check_schema(engine)  # no raise


# --- the server refuses to start ------------------------------------------


def test_server_refuses_to_start_behind_head():
    proc = subprocess.run(
        [sys.executable, "-m", "studysystem.server"],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 1
    assert "study migrate" in proc.stderr
    assert stamp() is None  # it did not migrate on its own
    assert not (data_dir() / "snapshots").exists()  # and stopped before snapshotting


# --- the migration builds the schema the code believes in -----------------
#
# Everything else in the suite runs on `metadata.create_all`. These run on
# `alembic upgrade head`, which is what a real install gets, and they run on both engines:
# a CHECK only SQLite accepts would pass every other test in the file. Autogenerate has
# already been caught dropping 128 column CHECKs and 3 expression indexes once (D-27); this
# is the test that catches the next one.


def reflected_indexes(engine) -> set[str]:
    """Index names, by hand on each engine.

    `Inspector.get_indexes` cannot see an expression index (`lower(code)`) on SQLite - the
    same blind spot that made autogenerate drop those three - so reading it would leave them
    unproven. Postgres lists an index behind every unique and primary key constraint too,
    hence the `ix_` / `ux_` filter: those are the ones this schema declares by hand.
    """
    if engine.dialect.name == "sqlite":
        query = "SELECT name FROM sqlite_master WHERE type = 'index' AND sql IS NOT NULL"
    else:
        query = "SELECT indexname FROM pg_indexes WHERE schemaname = 'public'"
    with engine.connect() as conn:
        names = {row[0] for row in conn.exec_driver_sql(query)}
    return {name for name in names if name.startswith(("ix_", "ux_"))}


def migrated(engine):
    migrate.upgrade(engine)
    return inspect(engine)


def test_migration_creates_every_table(empty_engine):
    assert set(migrated(empty_engine).get_table_names()) == set(metadata.tables) | {
        "alembic_version"
    }


def test_migration_creates_every_index(empty_engine):
    migrated(empty_engine)
    declared = {ix.name for table in metadata.tables.values() for ix in table.indexes}
    assert reflected_indexes(empty_engine) == declared


def test_migration_creates_every_check(empty_engine):
    """The one autogenerate silently got wrong, on both engines, by name."""
    inspector = migrated(empty_engine)
    built = {
        con["name"]
        for name in inspector.get_table_names()
        for con in inspector.get_check_constraints(name)
    }
    declared = {
        c.name
        for table in metadata.tables.values()
        for c in table.constraints
        if isinstance(c, CheckConstraint)
    }
    assert declared - built == set()
    assert len(built) == len(declared)


def test_the_migrated_index_refuses_a_semester_in_another_case(empty_engine):
    """D-40, on the migrated database: the index-name check above cannot see an expression."""
    migrate.upgrade(empty_engine)
    row = {
        "user_id": "0" * 26,
        "code": "I3302-E",
        "name": "Server-Side Web Development",
        "instructor_tier": "unknown",
        "credits_tier": "unknown",
        "created_at": "2026-09-26T00:00:00Z",
    }
    with empty_engine.begin() as conn:
        conn.execute(insert(user).values(id="0" * 26, created_at=row["created_at"]))
        conn.execute(insert(course).values(id="1" * 26, semester_name="Semester 1", **row))

    with pytest.raises(IntegrityError), empty_engine.begin() as conn:
        conn.execute(insert(course).values(id="2" * 26, semester_name="SEMESTER 1", **row))


# --- 0005: the first rebuild of a parent table (D-103) ----------------------


def test_0005_rebuilds_generation_task_under_its_children(empty_engine):
    """A task with a submission pointing at it survives the rebuild that widens `kind`, and the
    widened CHECK takes a tagging task. On SQLite the drop fails without D-103's switch."""
    command.upgrade(migrate.alembic_config(empty_engine), "0004")
    at = "2026-10-05T00:00:00Z"
    task = {
        "user_id": "0" * 26,
        "scope_type": "material",
        "scope_id": "9" * 26,
        "route": "host",
        "created_at": at,
    }
    with empty_engine.begin() as conn:
        conn.execute(insert(user).values(id="0" * 26, created_at=at))
        conn.execute(insert(generation_task).values(id="1" * 26, kind="note", **task))
        conn.execute(
            insert(submission).values(
                id="2" * 26,
                user_id="0" * 26,
                task_id="1" * 26,
                ordinal=1,
                payload="{}",
                validation_result="{}",
                accepted_count=0,
                rejected_count=0,
                bytes_sent=0,
                bytes_returned=0,
                created_at=at,
            )
        )

    migrate.upgrade(empty_engine)

    with empty_engine.begin() as conn:
        assert conn.execute(select(func.count()).select_from(submission)).scalar_one() == 1
        conn.execute(insert(generation_task).values(id="3" * 26, kind="tagging", **task))
    with pytest.raises(IntegrityError), empty_engine.begin() as conn:
        conn.execute(insert(generation_task).values(id="4" * 26, kind="bogus", **task))


def test_0006_rebuilds_study_session_under_its_children(empty_engine):
    """A session with an hours entry pointing at it survives the rebuild that adds `practice`,
    and the widened CHECK takes a practice session."""
    command.upgrade(migrate.alembic_config(empty_engine), "0005")
    at = "2026-10-07T00:00:00Z"
    uid = "0" * 26
    session = {
        "user_id": uid,
        "course_id": "1" * 26,
        "source": "system",
        "started_at": at,
        "expires_at": "2026-10-07T04:00:00Z",
    }
    with empty_engine.begin() as conn:
        conn.execute(insert(user).values(id=uid, created_at=at))
        conn.execute(
            insert(course).values(
                id="1" * 26,
                user_id=uid,
                code="I3302",
                name="Server-Side Web Development",
                semester_name="Semester 1",
                instructor_tier="unknown",
                credits_tier="unknown",
                created_at=at,
            )
        )
        conn.execute(insert(study_session).values(id="2" * 26, mode="mock", **session))
        conn.execute(
            insert(hours_entry).values(
                id="3" * 26,
                user_id=uid,
                course_id="1" * 26,
                session_id="2" * 26,
                minutes=40,
                occurred_at=at,
                source="tool",
                created_at=at,
            )
        )

    migrate.upgrade(empty_engine)

    with empty_engine.begin() as conn:
        assert conn.execute(select(func.count()).select_from(hours_entry)).scalar_one() == 1
        conn.execute(insert(study_session).values(id="4" * 26, mode="practice", **session))
    with pytest.raises(IntegrityError), empty_engine.begin() as conn:
        conn.execute(insert(study_session).values(id="5" * 26, mode="bogus", **session))


def test_migrate_leaves_foreign_keys_on():
    """D-103 switches them off for the run only: the pooled connection enforces them again."""
    engine = make_engine(db_path())
    migrate.upgrade(engine)
    with engine.connect() as conn:
        assert conn.exec_driver_sql("PRAGMA foreign_keys").scalar_one() == 1
    engine.dispose()
