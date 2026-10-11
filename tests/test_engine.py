import datetime
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest
from sqlalchemy import event
from sqlalchemy.exc import IntegrityError, OperationalError

from studysystem.db.engine import data_dir, db_path, make_engine, snapshot
from studysystem.services.units import read_unit, write_unit

WRITER = Path(__file__).with_name("_writer.py")


# --- data_dir / db_path ---------------------------------------------------


def test_env_var_wins_and_is_created(tmp_path, monkeypatch):
    nested = tmp_path / "a" / "b" / "c"
    monkeypatch.setenv("STUDYSYSTEM_DATA_DIR", str(nested))
    assert data_dir() == nested
    assert nested.is_dir()
    assert db_path() == nested / "studysystem.db"


def test_platformdirs_fallback(monkeypatch):
    monkeypatch.delenv("STUDYSYSTEM_DATA_DIR")
    assert data_dir().name == "studysystem"


# --- snapshot -------------------------------------------------------------

TODAY = datetime.date(2026, 9, 17)


def test_first_snapshot_of_the_day_writes_a_valid_db():
    engine = make_engine(db_path())
    target = snapshot(engine, TODAY)
    assert target is not None and target.exists()
    assert target == data_dir() / "snapshots" / "2026-09-17.db"
    with sqlite3.connect(target) as copy:
        assert copy.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_second_start_same_day_writes_nothing():
    engine = make_engine(db_path())
    assert snapshot(engine, TODAY) is not None
    assert snapshot(engine, TODAY) is None
    assert [p.name for p in (data_dir() / "snapshots").iterdir()] == ["2026-09-17.db"]


def test_next_day_writes_a_second_file():
    engine = make_engine(db_path())
    snapshot(engine, TODAY)
    snapshot(engine, TODAY + datetime.timedelta(days=1))
    names = sorted(p.name for p in (data_dir() / "snapshots").iterdir())
    assert names == ["2026-09-17.db", "2026-09-18.db"]


def test_snapshot_is_a_point_in_time_copy():
    engine = make_engine(db_path())
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE t (id INTEGER PRIMARY KEY)")
        conn.exec_driver_sql("INSERT INTO t DEFAULT VALUES")
    target = snapshot(engine, TODAY)
    with engine.begin() as conn:
        conn.exec_driver_sql("INSERT INTO t DEFAULT VALUES")
    with sqlite3.connect(target) as copy:
        assert copy.execute("SELECT count(*) FROM t").fetchone() == (1,)


# --- make_engine ----------------------------------------------------------


def test_two_processes_write_the_same_file():
    path = db_path()
    procs = [
        subprocess.Popen([sys.executable, str(WRITER), str(path)], stderr=subprocess.PIPE)
        for _ in range(2)
    ]
    results = [(p.wait(), p.stderr.read().decode()) for p in procs]
    assert all(code == 0 for code, _ in results), results
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT count(*) FROM smoke").fetchone() == (2,)


def test_foreign_keys_are_enforced():
    engine = make_engine(db_path())
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE parent (id INTEGER PRIMARY KEY)")
        conn.exec_driver_sql(
            "CREATE TABLE child (id INTEGER PRIMARY KEY, parent_id INTEGER REFERENCES parent(id))"
        )
    with pytest.raises(IntegrityError), engine.begin() as conn:
        conn.exec_driver_sql("INSERT INTO child (parent_id) VALUES (999)")


# --- after the independent review ---------------------------------------------


def test_a_snapshot_cut_short_leaves_nothing_under_the_days_name(monkeypatch):
    engine = make_engine(db_path())
    real = engine.raw_connection

    class CutShort:
        """VACUUM INTO that writes half a file, then fails - a full disk."""

        def __init__(self):
            self.raw = real()

        def execute(self, _sql, params):
            Path(params[0]).write_bytes(b"half a database")
            raise sqlite3.OperationalError("database or disk is full")

        def close(self):
            self.raw.close()

    monkeypatch.setattr(engine, "raw_connection", CutShort)
    today = datetime.date(2026, 10, 10)

    with pytest.raises(sqlite3.OperationalError):
        snapshot(engine, today)

    assert list((data_dir() / "snapshots").iterdir()) == []
    # so the next start still takes the day's copy, and it is a real database
    target = snapshot(make_engine(db_path()), today)
    with sqlite3.connect(target) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def _with_table(path: Path):
    engine = make_engine(path)
    with write_unit(engine) as conn:
        conn.exec_driver_sql("CREATE TABLE t (id INTEGER)")
    return engine


def test_a_read_does_not_wait_behind_another_write_unit():
    writer = _with_table(db_path())
    reader = make_engine(db_path())

    with write_unit(writer) as conn:
        conn.exec_driver_sql("INSERT INTO t VALUES (1)")  # the write lock is held from here
        started = time.perf_counter()
        with read_unit(reader) as rconn:
            assert rconn.exec_driver_sql("SELECT count(*) FROM t").scalar_one() == 0
        assert time.perf_counter() - started < 1  # not the 5 s busy_timeout


def test_a_second_write_unit_waits_for_the_lock():
    writer = _with_table(db_path())
    other = make_engine(db_path())

    @event.listens_for(other, "connect")
    def quick(dbapi_conn, _record):  # after make_engine's: 100 ms instead of 5 s
        dbapi_conn.execute("PRAGMA busy_timeout = 100")

    with write_unit(writer) as conn:
        conn.exec_driver_sql("INSERT INTO t VALUES (1)")
        # BEGIN IMMEDIATE asks for the lock before the first statement, so the unit cannot open
        with pytest.raises(OperationalError, match="locked"), write_unit(other):
            pass
