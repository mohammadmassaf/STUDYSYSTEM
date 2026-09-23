"""SQLite engine bootstrap: where the file lives, how every connection is configured,
and the once-a-day snapshot."""

import datetime
import os
import sqlite3
import time
from pathlib import Path

import platformdirs
from sqlalchemy import Engine, create_engine, event

# Ten tries, 50ms apart: the holder only needs to finish one PRAGMA.
_WAL_ATTEMPTS = 10
_WAL_BACKOFF = 0.05


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


def _ensure_wal(dbapi_conn) -> None:
    """Put the file in WAL, tolerating another process doing the same thing.

    `foreign_keys` and `busy_timeout` are settings of the connection, so every connection sets
    them. `journal_mode` is not: it lives in the file header and outlives the connection that
    wrote it, so all this has to do is get the file there once.

    Switching takes an exclusive lock, and SQLite answers SQLITE_BUSY straight away rather than
    waiting out `busy_timeout` - so two processes opening a new file at the same moment is a
    real race, not a slow path. Reading the mode first skips the lock entirely once the file is
    WAL, which is every connection after the first; the retry covers the first.
    """
    for _ in range(_WAL_ATTEMPTS):
        if dbapi_conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal":
            return
        try:
            dbapi_conn.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError:
            time.sleep(_WAL_BACKOFF)  # the holder is mid-switch; it lands in milliseconds
    raise RuntimeError(
        "could not put the database in WAL mode: another process held it for "
        f"{_WAL_ATTEMPTS * _WAL_BACKOFF:.1f}s"
    )


def make_engine(path: Path) -> Engine:
    engine = create_engine(f"sqlite:///{path.as_posix()}", connect_args={"isolation_level": None})

    @event.listens_for(engine, "connect")
    def on_connect(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys = ON")
        dbapi_conn.execute("PRAGMA busy_timeout = 5000")
        _ensure_wal(dbapi_conn)

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
