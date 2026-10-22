import datetime
import sqlite3
import subprocess
import sys
from types import SimpleNamespace

import pytest
from alembic import command
from click.testing import CliRunner
from sqlalchemy import CheckConstraint, func, insert, inspect, select
from sqlalchemy.exc import IntegrityError

from studysystem import cli
from studysystem.cli import study
from studysystem.db import migrate
from studysystem.db.engine import data_dir, db_path, make_engine, snapshot
from studysystem.db.tables import (
    assessment,
    assessment_slot,
    course,
    exam_profile,
    generation_task,
    hours_entry,
    metadata,
    past_exam,
    plan,
    plan_item,
    study_session,
    submission,
    user,
)
from tests._study import run_study

HEAD = "0015"


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


def _freeze_cli_clock(monkeypatch) -> datetime.datetime:
    """09:30:00 on 2026-10-11, local. `study` reads the clock through cli's own `datetime` name,
    so the fixed time is set there only - the stdlib module every other import shares is left
    alone. A real clock could name one copy on each side of midnight, or of a second."""
    at = datetime.datetime(2026, 10, 11, 9, 30).astimezone()  # noqa: DTZ001 - local wall clock

    class Frozen(datetime.datetime):
        @classmethod
        def now(cls, tz=None):
            return at if tz is None else at.astimezone(tz)

    monkeypatch.setattr(cli, "datetime", SimpleNamespace(datetime=Frozen))
    return at


def test_migrate_snapshots_even_when_daily_snapshot_exists(monkeypatch):
    at = _freeze_cli_clock(monkeypatch)
    engine = make_engine(db_path())
    assert snapshot(engine, at.date()) is not None  # the server already ran today
    assert CliRunner().invoke(study, ["migrate"]).exit_code == 0
    names = {p.name for p in (data_dir() / "snapshots").iterdir()}
    assert names == {"2026-10-11.db", "2026-10-11-premigrate-093000.db"}


def test_two_runs_in_one_second_keep_a_copy_each(monkeypatch):
    """migrate, downgrade, migrate inside 09:30:00: the second migrate's copy is the only one
    taken after the downgrade, so it gets its own name instead of being skipped."""
    _freeze_cli_clock(monkeypatch)
    runner = CliRunner()
    for args in (["migrate"], ["downgrade", "0008"], ["migrate"]):
        result = runner.invoke(study, args)
        assert result.exit_code == 0, result.output
        assert "snapshot: None" not in result.output

    names = {p.name for p in (data_dir() / "snapshots").iterdir()}
    assert names == {
        "2026-10-11-premigrate-093000.db",
        "2026-10-11-predowngrade-093000.db",
        "2026-10-11-premigrate-093000-2.db",
    }
    with sqlite3.connect(data_dir() / "snapshots" / "2026-10-11-premigrate-093000-2.db") as db:
        assert db.execute("SELECT version_num FROM alembic_version").fetchone() == ("0008",)


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


# --- after the independent review -------------------------------------------------------------


def test_a_run_that_leaves_a_dangling_row_rolls_back_whole():
    """D-103's check runs before the run's one COMMIT: a dangling row found after 0008 and 0009
    undoes both, not just the last. SQLite only - Postgres enforces foreign keys throughout."""
    engine = make_engine(db_path())
    command.upgrade(migrate.alembic_config(engine), "0007")
    with sqlite3.connect(db_path()) as db:  # plain sqlite3: foreign keys off, as during a rebuild
        db.execute(
            "INSERT INTO course (id, user_id, code, name, semester_name, instructor_tier,"
            " credits_tier, created_at) VALUES (?, ?, 'I3302', 'Databases', 'Fall 2026',"
            " 'unknown', 'unknown', '2026-10-10T00:00:00Z')",
            ("1" * 26, "9" * 26),  # a user that does not exist
        )

    with pytest.raises(RuntimeError, match="missing ids"):
        migrate.upgrade(engine)

    assert stamp() == "0007"


def test_check_schema_names_an_empty_database_as_such():
    with pytest.raises(migrate.SchemaBehindHead) as excinfo:
        migrate.check_schema(make_engine(db_path()))

    assert excinfo.value.message == f"database has no schema yet, head is {HEAD}"


def _stamp_unknown_revision(engine) -> None:
    """The database as a newer build left it: a revision this build has never heard of."""
    migrate.upgrade(engine)
    with sqlite3.connect(db_path()) as db:
        db.execute("UPDATE alembic_version SET version_num = 'ffff'")


def test_check_schema_refuses_a_database_ahead_of_head():
    engine = make_engine(db_path())
    _stamp_unknown_revision(engine)

    with pytest.raises(migrate.SchemaAheadOfHead) as excinfo:
        migrate.check_schema(engine)

    err = excinfo.value
    assert err.code == "schema_ahead_of_head"
    assert "study migrate" not in err.fix  # migrate cannot help here
    assert (err.current, err.head) == ("ffff", HEAD)


def test_migrate_on_a_database_ahead_of_head_fails_cleanly():
    _stamp_unknown_revision(make_engine(db_path()))

    result = CliRunner().invoke(study, ["migrate"])

    assert result.exit_code == 1
    assert "ffff" in result.output
    assert "Traceback" not in result.output
    assert stamp() == "ffff"


def test_downgrade_snapshots_first():
    runner = CliRunner()
    assert runner.invoke(study, ["migrate"]).exit_code == 0

    result = runner.invoke(study, ["downgrade", "0008"])

    assert result.exit_code == 0, result.output
    names = [p.name for p in (data_dir() / "snapshots").iterdir()]
    assert any("-predowngrade-" in n for n in names)
    assert stamp() == "0008"


@pytest.mark.parametrize("target", ["9999", "-99"])  # a revision typo, a step count too far
def test_downgrade_to_an_unknown_target_changes_nothing(target):
    runner = CliRunner()
    assert runner.invoke(study, ["migrate"]).exit_code == 0

    result = runner.invoke(study, ["downgrade", "--", target])  # `--`: -99 is not an option

    assert result.exit_code == 1
    assert target in result.output
    assert stamp() == HEAD
    names = [p.name for p in (data_dir() / "snapshots").iterdir()]
    assert not any("-predowngrade-" in n for n in names)  # the refused run keeps no copy


def test_downgrade_on_a_missing_database_creates_nothing():
    result = CliRunner().invoke(study, ["downgrade", "base"])

    assert result.exit_code == 1
    assert "no database at" in result.output
    assert not db_path().exists()


def _locked_rollback_file() -> sqlite3.Connection:
    """A migrated file in the old journal mode, held under an exclusive lock: even a read waits
    out busy_timeout (5 s) and fails. The caller closes the holder."""
    engine = make_engine(db_path())
    migrate.upgrade(engine)
    engine.dispose()
    holder = sqlite3.connect(db_path(), isolation_level=None)
    holder.execute("PRAGMA journal_mode = DELETE")  # under WAL a reader never waits
    holder.execute("BEGIN EXCLUSIVE")
    return holder


def test_migrate_refuses_a_locked_database_in_one_line():
    holder = _locked_rollback_file()
    try:
        result = CliRunner().invoke(study, ["migrate"])
    finally:
        holder.close()

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)  # a click refusal, not a traceback
    assert "database_unavailable: cannot open" in result.output
    assert "database is locked" in result.output
    assert not (data_dir() / "snapshots").exists()  # refused before the copy


def test_migrate_while_another_program_writes_is_busy_and_changes_nothing():
    """The dashboard holds the write lock: with WAL the read goes through, the upgrade's write
    waits out busy_timeout. One line, and the run's one transaction left 0008 as it was."""
    engine = make_engine(db_path())
    command.upgrade(migrate.alembic_config(engine), "0008")
    engine.dispose()
    holder = sqlite3.connect(db_path(), isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        result = CliRunner().invoke(study, ["migrate"])
    finally:
        holder.close()

    assert result.exit_code == 1
    assert isinstance(result.exception, SystemExit)
    assert "database_busy: another program is writing to" in result.output
    assert stamp() == "0008"


def test_downgrade_while_another_program_writes_is_busy_and_keeps_no_copy():
    runner = CliRunner()
    assert runner.invoke(study, ["migrate"]).exit_code == 0
    holder = sqlite3.connect(db_path(), isolation_level=None)
    holder.execute("BEGIN IMMEDIATE")
    try:
        result = runner.invoke(study, ["downgrade", "0008"])
    finally:
        holder.close()

    assert result.exit_code == 1
    assert "database_busy: another program is writing to" in result.output
    assert stamp() == HEAD
    names = [p.name for p in (data_dir() / "snapshots").iterdir()]
    assert not any("-predowngrade-" in n for n in names)


def test_migrate_keeps_any_other_database_error_a_crash(monkeypatch):
    """Only the lock wait becomes database_busy: a broken migration must not read as busy."""
    from sqlalchemy.exc import OperationalError

    def broken(_engine):
        raise OperationalError("ALTER TABLE", {}, sqlite3.OperationalError("no such table: x"))

    monkeypatch.setattr(migrate, "upgrade", broken)

    result = CliRunner().invoke(study, ["migrate"])

    assert isinstance(result.exception, OperationalError)
    assert "database_busy" not in result.output


def test_downgrade_takes_a_relative_target():
    runner = CliRunner()
    assert runner.invoke(study, ["migrate"]).exit_code == 0

    result = runner.invoke(study, ["downgrade", "head-1"])

    assert result.exit_code == 0, result.output
    assert stamp() == f"{int(HEAD) - 1:04d}"


def test_a_failed_daily_snapshot_does_not_stop_the_server(monkeypatch, caplog):
    import studysystem.server as server
    import studysystem.startup as startup

    migrate.upgrade(make_engine(db_path()))
    started = []

    def disk_full(*_args):
        raise OSError("No space left on device")

    class Server:
        def run(self):
            started.append(True)

    monkeypatch.setattr(startup, "snapshot", disk_full)
    monkeypatch.setattr(server, "create_server", lambda engine: Server())

    server.main()

    assert started == [True]
    assert "daily snapshot failed" in caplog.text


# --- ON DELETE RESTRICT, as the migrations built it (Conventions) ---------------------


def test_every_migrated_foreign_key_restricts_deletes(empty_engine):
    """Read back from the database the nine migrations built, not from `metadata`: a rebuilt
    table (0005, 0006, 0008) that lost its `ON DELETE RESTRICT` would pass a metadata-only check."""
    inspector = migrated(empty_engine)
    built = [
        (name, tuple(fk["constrained_columns"]), (fk.get("options") or {}).get("ondelete"))
        for name in inspector.get_table_names()
        for fk in inspector.get_foreign_keys(name)
    ]
    declared = sum(len(table.foreign_keys) for table in metadata.tables.values())

    assert len(built) == declared
    assert [b for b in built if (b[2] or "").upper() != "RESTRICT"] == []


def test_a_course_with_a_slot_cannot_be_deleted(empty_engine):
    """I3302 and its default Final slot on the migrated database: the delete is refused and the
    course stays. On SQLite this also proves the runtime connection has foreign keys on."""
    from studysystem.services.courses import add_course
    from studysystem.services.users import ensure_user

    migrate.upgrade(empty_engine)
    uid = ensure_user(empty_engine)
    web = add_course(empty_engine, uid, "I3302", "Server-side WEB Development", "Semester 1")

    with pytest.raises(IntegrityError), empty_engine.begin() as conn:
        conn.execute(course.delete().where(course.c.id == web["course_id"]))

    with empty_engine.connect() as conn:
        assert conn.execute(select(course.c.id)).scalars().all() == [web["course_id"]]


# --- 0007 - 0009 with rows present (his database went through all three full) ----------------

AT = "2026-10-08T00:00:00Z"
UID, COURSE, SLOT, EXAM, PAPER, PLAN = ("0" * 26, "1" * 26, "2" * 26, "3" * 26, "4" * 26, "5" * 26)


def _setup_item(item_id: str, kind: str, rank: int) -> dict:
    return {
        "id": item_id,
        "user_id": UID,
        "plan_id": PLAN,
        "lane": "setup",
        "course_id": COURSE,
        "rank": rank,
        "reason": f"{kind} for I3302",
        "setup_kind": kind,
        "setup_key": f"{kind}:{SLOT}",
    }


def _i3302_at_0006(engine) -> None:
    """I3302 at 0006: its Final (slot + sitting), the Final 2020-02-17 paper, and a plan with a
    past-papers setup item - one row in each table 0007, 0008 and 0009 change."""
    command.upgrade(migrate.alembic_config(engine), "0006")
    with engine.begin() as conn:
        conn.execute(insert(user).values(id=UID, created_at=AT))
        conn.execute(
            insert(course).values(
                id=COURSE,
                user_id=UID,
                code="I3302",
                name="Server-Side Web Development",
                semester_name="Semester 1",
                instructor_tier="unknown",
                credits_tier="unknown",
                created_at=AT,
            )
        )
        conn.execute(
            insert(assessment_slot).values(
                id=SLOT,
                owner_id=UID,
                course_id=COURSE,
                name="Final exam",
                kind="exam",
                created_at=AT,
            )
        )
        conn.execute(
            insert(assessment).values(
                id=EXAM,
                user_id=UID,
                slot_id=SLOT,
                weight=100,
                weight_tier="inferred",
                created_at=AT,
            )
        )
        conn.execute(
            insert(past_exam).values(
                id=PAPER,
                owner_id=UID,
                slot_id=SLOT,
                session_type="first",
                session_date="2020-02-17",
                instructor_tier="unknown",
                file_ref="files/x",
                content_sha256="a" * 64,
                created_at=AT,
            )
        )
        conn.execute(
            insert(plan).values(
                id=PLAN,
                user_id=UID,
                scope="all-courses",
                shown_n=5,
                strategy_snapshot="{}",
                created_at=AT,
            )
        )
        conn.execute(insert(plan_item).values(**_setup_item("6" * 26, "past-papers", 1)))


def test_0007_to_0009_keep_his_rows_and_leave_unknown_unknown(empty_engine):
    _i3302_at_0006(empty_engine)

    migrate.upgrade(empty_engine)

    with empty_engine.begin() as conn:
        exam = conn.execute(select(assessment).where(assessment.c.id == EXAM)).one()
        paper = conn.execute(select(past_exam).where(past_exam.c.id == PAPER)).one()
        kinds = conn.execute(select(plan_item.c.setup_kind)).scalars().all()
        conn.execute(insert(plan_item).values(**_setup_item("7" * 26, "more-papers", 2)))
    assert (exam.weight, exam.prep_from) == (100, None)  # no prep day guessed (0007)
    assert (paper.session_date, paper.session_date_approx) == ("2020-02-17", 0)  # exact (0009)
    assert kinds == ["past-papers"]  # survived the plan_item rebuild (0008)
    with pytest.raises(IntegrityError, match="setup_kind"), empty_engine.begin() as conn:
        conn.execute(insert(plan_item).values(**_setup_item("8" * 26, "bogus", 3)))


def test_downgrade_below_0008_is_refused_while_a_more_papers_item_exists(empty_engine):
    """0008's promise: the old CHECK refuses the row, so the downgrade fails and changes nothing."""
    _i3302_at_0006(empty_engine)
    migrate.upgrade(empty_engine)
    with empty_engine.begin() as conn:
        conn.execute(insert(plan_item).values(**_setup_item("7" * 26, "more-papers", 2)))

    with pytest.raises(IntegrityError, match="setup_kind"):
        migrate.downgrade(empty_engine, "0007")

    assert migrate.current_revision(empty_engine) == HEAD
    with empty_engine.connect() as conn:
        kinds = sorted(conn.execute(select(plan_item.c.setup_kind)).scalars().all())
    assert kinds == ["more-papers", "past-papers"]
    columns = {c["name"] for c in inspect(empty_engine).get_columns("past_exam")}
    assert "session_date_approx" in columns  # 0009's drop ran first, in the same transaction


def test_migrate_twice_leaves_exactly_one_user():
    runner = CliRunner()
    assert runner.invoke(study, ["migrate"]).exit_code == 0
    assert runner.invoke(study, ["migrate"]).exit_code == 0

    with sqlite3.connect(db_path()) as db:
        assert db.execute("SELECT count(*) FROM user").fetchone() == (1,)


# --- `study serve`, the way Claude Desktop starts it ---------------------------------------


def serve() -> subprocess.CompletedProcess:
    return run_study("serve")


def test_study_serve_refuses_an_empty_database():
    proc = serve()

    assert proc.returncode == 1
    assert f"database has no schema yet, head is {HEAD}" in proc.stderr
    assert "run: study migrate" in proc.stderr
    assert not db_path().exists()  # refused before opening it: no empty file left behind


def test_study_serve_refuses_a_locked_database_in_one_line():
    """Another process holds the file's exclusive lock past busy_timeout (5 s): one line naming
    the code and the fix, not a traceback."""
    holder = _locked_rollback_file()
    try:
        proc = serve()
    finally:
        holder.close()

    assert proc.returncode == 1
    assert "database_unavailable: cannot open" in proc.stderr
    assert "database is locked" in proc.stderr
    assert "Traceback" not in proc.stderr


def test_study_serve_refuses_a_file_that_is_not_a_database_in_one_line():
    db_path().write_text("not a database, a typed note saved under the wrong name")

    proc = serve()

    assert proc.returncode == 1
    assert "database_unavailable: cannot open" in proc.stderr
    assert "file is not a database" in proc.stderr
    assert "snapshots folder" in proc.stderr  # the damaged-file fix
    assert "Traceback" not in proc.stderr


def test_a_failed_wal_switch_refuses_the_server_in_one_line(monkeypatch, capsys):
    import studysystem.db.engine as engine_module
    import studysystem.server as server

    engine = make_engine(db_path())
    migrate.upgrade(engine)
    engine.dispose()

    def held(_conn):
        raise engine_module.WalUnavailable("could not put the database in WAL mode")

    monkeypatch.setattr(engine_module, "_ensure_wal", held)  # what the real connect hook calls

    with pytest.raises(SystemExit) as excinfo:
        server.main()

    assert excinfo.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("database_unavailable: cannot open")
    assert "WAL mode" in err


def test_study_serve_refuses_a_database_behind_head_and_leaves_its_rows():
    """I3302 at 0008, one migration behind: refused, not migrated, not snapshotted."""
    engine = make_engine(db_path())
    command.upgrade(migrate.alembic_config(engine), "0008")
    with engine.begin() as conn:
        conn.execute(insert(user).values(id=UID, created_at=AT))
        conn.execute(
            insert(course).values(
                id=COURSE,
                user_id=UID,
                code="I3302",
                name="Server-Side Web Development",
                semester_name="Semester 1",
                instructor_tier="unknown",
                credits_tier="unknown",
                created_at=AT,
            )
        )
    engine.dispose()

    proc = serve()

    assert proc.returncode == 1
    assert f"database is at 0008, head is {HEAD}" in proc.stderr
    assert "run: study migrate" in proc.stderr
    assert stamp() == "0008"
    with sqlite3.connect(db_path()) as db:
        assert db.execute("SELECT code FROM course").fetchall() == [("I3302",)]
    assert not (data_dir() / "snapshots").exists()


def test_study_serve_refuses_a_database_ahead_of_head():
    engine = make_engine(db_path())
    _stamp_unknown_revision(engine)
    engine.dispose()

    proc = serve()

    assert proc.returncode == 1
    assert "schema_ahead_of_head" in proc.stderr
    assert "study migrate" not in proc.stderr
    assert stamp() == "ffff"
    assert not (data_dir() / "snapshots").exists()


# --- 0010 with rows present (his database holds 52 tasks, one open) ---------------------------


def _task(task_id: str, status: str) -> dict:
    return {
        "id": task_id,
        "user_id": UID,
        "kind": "tagging",
        "scope_type": "material",
        "scope_id": "9" * 26,
        "route": "host",
        "status": status,
        "closed_at": None if status == "open" else AT,
        "created_at": AT,
    }


def test_0010_counts_a_closed_task_fetched_and_an_open_one_not(empty_engine):
    """Every task so far went out as one reply: a closed one got it, the open one (I3304's
    tagging, cut by Cowork) must fetch again before its submit (D-145)."""
    command.upgrade(migrate.alembic_config(empty_engine), "0009")
    with empty_engine.begin() as conn:
        conn.execute(insert(user).values(id=UID, created_at=AT))
        conn.execute(insert(generation_task).values(**_task("a" * 26, "closed")))
        conn.execute(insert(generation_task).values(**_task("b" * 26, "open")))

    migrate.upgrade(empty_engine)

    with empty_engine.connect() as conn:
        rows = conn.execute(
            select(
                generation_task.c.status,
                generation_task.c.parts,
                generation_task.c.parts_sent,
                generation_task.c.layout_hash,
            ).order_by(generation_task.c.id)
        ).all()
    assert [tuple(r) for r in rows] == [("closed", 1, 1, None), ("open", 1, 0, None)]


# --- 0011: the observation kind (D-149) ---------------------------------------------------------


def test_0011_takes_an_observation_task_and_keeps_the_ones_before(empty_engine):
    """A transcription task made before 0011 survives the rebuild that widens `kind`, the widened
    CHECK takes an observation task, and the downgrade is refused while one exists."""
    command.upgrade(migrate.alembic_config(empty_engine), "0010")
    paper = {**_task("a" * 26, "closed"), "kind": "transcription", "scope_type": "past-exam"}
    with empty_engine.begin() as conn:
        conn.execute(insert(user).values(id=UID, created_at=AT))
        conn.execute(insert(generation_task).values(**paper))

    migrate.upgrade(empty_engine)

    with empty_engine.begin() as conn:
        conn.execute(
            insert(generation_task).values(**{**paper, "id": "b" * 26, "kind": "observation"})
        )
        kinds = conn.execute(select(generation_task.c.kind).order_by(generation_task.c.id))
        assert kinds.scalars().all() == ["transcription", "observation"]
    with pytest.raises(IntegrityError):
        command.downgrade(migrate.alembic_config(empty_engine), "0010")


# --- 0012: profile_claim (D-156) ----------------------------------------------------------------


def test_0012_adds_profile_claim_and_its_downgrade_drops_only_it(empty_engine):
    """The table's shape is proved by the every-table / every-check / restrict tests above; here,
    the step itself: 0011 has no `profile_claim`, 0012 adds it, and down to 0011 removes it alone
    - its rows are derived, the next derive writes them again (D-156)."""
    command.upgrade(migrate.alembic_config(empty_engine), "0011")
    before = set(inspect(empty_engine).get_table_names())
    assert "profile_claim" not in before

    migrate.upgrade(empty_engine)
    assert set(inspect(empty_engine).get_table_names()) == before | {"profile_claim"}

    command.downgrade(migrate.alembic_config(empty_engine), "0011")
    assert set(inspect(empty_engine).get_table_names()) == before


# --- 0013: the assumed-instructor count (D-176) -------------------------------------------------


def test_0013_keeps_a_profile_and_its_task_and_reads_it_as_assuming_none(empty_engine):
    """The rebuild of `exam_profile`, a parent table, keeps a profile made before it and the task
    built on it; the new count reads 0 - true, derive assumed no instructor before 2.4 (D-176) -
    and down to 0012 drops the column alone."""
    command.upgrade(migrate.alembic_config(empty_engine), "0012")
    profile = "6" * 26
    with empty_engine.begin() as conn:
        conn.execute(insert(user).values(id=UID, created_at=AT))
        conn.execute(
            insert(course).values(
                id=COURSE,
                user_id=UID,
                code="I3302",
                name="Server-Side Web Development",
                semester_name="Semester 1",
                instructor_tier="unknown",
                credits_tier="unknown",
                created_at=AT,
            )
        )
        conn.execute(
            insert(assessment_slot).values(
                id=SLOT,
                owner_id=UID,
                course_id=COURSE,
                name="Final exam",
                kind="exam",
                created_at=AT,
            )
        )
        conn.execute(
            insert(exam_profile).values(
                id=profile,
                owner_id=UID,
                slot_id=SLOT,
                version=1,
                derived_at=AT,
                provisional_syllabus=0,
                provisional_instructor=1,
                provisional_format=1,
                evidence_count_syllabus=5,
                evidence_count_instructor=0,
                evidence_count_format=0,
            )
        )
        conn.execute(
            insert(generation_task).values(
                **{**_task("a" * 26, "open"), "exam_profile_id": profile}
            )
        )

    migrate.upgrade(empty_engine)

    with empty_engine.connect() as conn:
        assert conn.execute(
            select(exam_profile.c.id, exam_profile.c.assumed_instructor_papers)
        ).all() == [(profile, 0)]
        assert conn.execute(select(generation_task.c.exam_profile_id)).scalars().all() == [profile]

    command.downgrade(migrate.alembic_config(empty_engine), "0012")
    columns = {c["name"] for c in inspect(empty_engine).get_columns("exam_profile")}
    assert "assumed_instructor_papers" not in columns
    assert "provisional_instructor" in columns


# --- 0014: a scan's confirmation and an item's page (D-210) --------------------------------------


def test_0014_stamps_a_transcribed_paper_at_its_tasks_close_and_leaves_the_rest_null(empty_engine):
    """A paper transcribed before the column counted at submit, so it must keep counting: its stamp
    is its task's close time. An untranscribed paper stays NULL, and down to 0013 drops both
    columns."""
    command.upgrade(migrate.alembic_config(empty_engine), "0013")
    done, waiting = "7" * 26, "8" * 26
    with empty_engine.begin() as conn:
        conn.execute(insert(user).values(id=UID, created_at=AT))
        conn.execute(
            insert(course).values(
                id=COURSE,
                user_id=UID,
                code="I3301",
                name="Computer Architecture",
                semester_name="Semester 1",
                instructor_tier="unknown",
                credits_tier="unknown",
                created_at=AT,
            )
        )
        conn.execute(
            insert(assessment_slot).values(
                id=SLOT, owner_id=UID, course_id=COURSE, name="Final", kind="exam", created_at=AT
            )
        )
        conn.execute(insert(generation_task).values(**_task("a" * 26, "closed")))
        for paper, sha, task in ((done, "b", "a" * 26), (waiting, "c", None)):
            conn.execute(
                insert(past_exam).values(
                    id=paper,
                    owner_id=UID,
                    slot_id=SLOT,
                    session_type="first",
                    session_date="2022-04-13",
                    instructor_tier="unknown",
                    file_ref="files/x",
                    content_sha256=sha * 64,
                    transcript_task_id=task,
                    created_at=AT,
                )
            )

    migrate.upgrade(empty_engine)

    with empty_engine.connect() as conn:
        stamps = dict(
            conn.execute(select(past_exam.c.id, past_exam.c.transcript_confirmed_at)).all()
        )
    assert stamps == {done: AT, waiting: None}

    command.downgrade(migrate.alembic_config(empty_engine), "0013")
    assert "transcript_confirmed_at" not in {
        c["name"] for c in inspect(empty_engine).get_columns("past_exam")
    }
    assert "page" not in {c["name"] for c in inspect(empty_engine).get_columns("practice_item")}


# --- 0015: a practice task's item count (D-211) ---------------------------------------------------


def test_0015_adds_a_nullable_item_count_and_down_to_0014_drops_it(empty_engine):
    migrate.upgrade(empty_engine)

    (column,) = [
        c for c in inspect(empty_engine).get_columns("generation_task") if c["name"] == "item_count"
    ]
    assert column["nullable"] is True

    command.downgrade(migrate.alembic_config(empty_engine), "0014")
    assert "item_count" not in {
        c["name"] for c in inspect(empty_engine).get_columns("generation_task")
    }
