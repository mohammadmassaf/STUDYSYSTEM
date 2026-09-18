import datetime
import sqlite3
import subprocess
import sys

import pytest
from click.testing import CliRunner

from studysystem.cli import study
from studysystem.db import migrate
from studysystem.db.engine import data_dir, db_path, make_engine, snapshot

HEAD = "0000"


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
