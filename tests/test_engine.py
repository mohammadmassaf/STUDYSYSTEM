import datetime
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from studysystem.db.engine import data_dir, db_path, make_engine, snapshot

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
