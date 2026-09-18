"""SQLite engine bootstrap: where the file lives, how every connection is configured,
and the once-a-day snapshot."""

import datetime
import os
from pathlib import Path

import platformdirs
from sqlalchemy import Engine, create_engine, event


def data_dir() -> Path:

    out = os.environ.get("STUDYSYSTEM_DATA_DIR")
    if out:
        path = Path(out)
    else:
        path = Path(platformdirs.user_data_dir("studysystem", appauthor=False))
    path.mkdir(parents=True, exist_ok=True)
    return path


def db_path() -> Path:
    return data_dir() / "studysystem.db"


def make_engine(path: Path) -> Engine:
    engine = create_engine(f"sqlite:///{path.as_posix()}", connect_args={"isolation_level": None})

    @event.listens_for(engine, "connect")
    def on_connect(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys = ON")
        dbapi_conn.execute("PRAGMA journal_mode = WAL")
        dbapi_conn.execute("PRAGMA busy_timeout = 5000")

    @event.listens_for(engine, "begin")
    def on_begin(conn):
        conn.exec_driver_sql("BEGIN IMMEDIATE")

    return engine


def snapshot(engine: Engine, today: datetime.date, suffix: str = "") -> Path | None:
    """Copy the database to snapshots/<date><suffix>.db. Skips if that file exists, which is
    the once-a-day rule for the plain startup snapshot (D-22)."""
    target = data_dir() / "snapshots" / f"{today.isoformat()}{suffix}.db"
    target.parent.mkdir(parents=True, exist_ok=True)

    if target.exists():
        return None

    raw = engine.raw_connection()
    try:
        raw.execute("VACUUM INTO ?", (target.as_posix(),))
    finally:
        raw.close()

    return target
