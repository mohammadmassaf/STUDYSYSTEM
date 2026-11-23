"""What every front door runs before it serves: the MCP server (`study serve`) and the dashboard
route (`study dashboard`) open the database the same way: build it if it is brand-new (D-324,
D-335), refuse it for the same reasons - lost (D-337), behind head, ahead of head, locked,
damaged (D-22, D-36) - then take the day's snapshot."""

import datetime
import logging
import sys
from pathlib import Path
from typing import NoReturn

from sqlalchemy import Engine
from sqlalchemy.exc import DatabaseError

from studysystem.db.engine import (
    WalUnavailable,
    db_path,
    has_snapshot,
    make_engine,
    snapshot,
    unavailable,
)
from studysystem.db.migrate import (
    SchemaAheadOfHead,
    SchemaBehindHead,
    check_schema,
    has_tables,
    upgrade,
)
from studysystem.errors import StudyError
from studysystem.services.users import current_user, ensure_user


def _refuse(e: StudyError) -> NoReturn:
    print(f"{e.code}: {e.message}. {e.fix}", file=sys.stderr)
    sys.exit(1)


def _missing(path: Path) -> StudyError:
    """No data where this machine had some. Building an empty database here would put the next
    hours in a second file, lost when the first is restored (D-337)."""
    return StudyError(
        code="database_missing",
        message=f"{path} is missing or empty, but the snapshots folder beside it holds copies or "
        "cannot be read",
        fix="put the file back or restore a copy from that folder; to start over with an empty "
        "database, run: study migrate",
    )


def _make_missing_user(engine: Engine) -> None:
    """The one user, when a first start was killed before making it. Reads first: `ensure_user`
    takes the write lock, and a start on a ready database must not wait for a writer. Two users
    stay each tool's `too_many_users` error (D-31)."""
    try:
        current_user(engine)
    except StudyError as e:
        if e.code == "no_user":
            ensure_user(engine)


def open_database() -> Engine:
    """The engine on the data dir's database, at head - or exit with one line naming the fix. A
    brand-new database is built first; one that has tables is never migrated here (D-324)."""
    path = db_path()
    # Read before the tables: another first start takes its copy only once its tables are saved,
    # so a copy seen here is never from a build this start still sees as empty.
    had_data = has_snapshot()
    if had_data and not path.exists():
        _refuse(_missing(path))  # before opening it, which would leave an empty file behind
    engine = make_engine(path)
    try:
        # No tables, not no file: a second program may have just created the file and still be
        # building in it. Both then run the upgrade; the write lock makes the later one wait, and
        # it finds nothing left to do.
        if not has_tables(engine):
            if had_data:
                _refuse(_missing(path))
            upgrade(engine)
        check_schema(engine)
        _make_missing_user(engine)
    except (SchemaBehindHead, SchemaAheadOfHead) as e:  # each names its own fix
        _refuse(e)
    except (DatabaseError, WalUnavailable) as e:  # the first connection could not use the file
        _refuse(unavailable(path, e))
    try:
        snapshot(engine, datetime.datetime.now().astimezone().date())
    except Exception:
        # A restore point is a convenience: a full disk or a read-only folder must not keep the
        # server from starting. The day's copy is retried at the next start.
        logging.getLogger("studysystem").warning("daily snapshot failed", exc_info=True)
    return engine
