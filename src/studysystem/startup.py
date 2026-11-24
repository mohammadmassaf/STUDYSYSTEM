"""What every front door runs before it serves: the MCP server (`study serve`) and the dashboard
route (`study dashboard`) open the database the same way: build it if it is brand-new (D-324,
D-335), refuse it for the same reasons - lost (D-337), ahead of head, locked, damaged (D-22, D-36)
- then take the day's snapshot. Behind head it opens untouched: every call refuses (D-338)."""

import datetime
import logging
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn

from sqlalchemy import Engine
from sqlalchemy.exc import DatabaseError

from studysystem.db.engine import (
    HOW_TO_RESTORE,
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
    schema_check,
    upgrade,
)
from studysystem.errors import StudyError
from studysystem.services.users import current_user, ensure_user


def _refuse(e: StudyError) -> NoReturn:
    print(f"{e.code}: {e.message}. {e.fix}", file=sys.stderr)
    sys.exit(1)


def _missing(path: Path, had: str) -> StudyError:
    """No data where this machine had some; `had` says how that is known. Building an empty
    database here would put the next hours in a second file, lost when the first is restored
    (D-337)."""
    return StudyError(
        code="database_missing",
        message=f"{path} is missing or empty, but {had}",
        fix="put the file back, with its studysystem.db-wal if it has one; or restore a copy from "
        f"the snapshots folder beside it: {HOW_TO_RESTORE}; or, to start over with an empty "
        "database, run: study migrate",
    )


HAD_COPIES = "the snapshots folder beside it holds copies or cannot be read"
HAD_OPENED = "this server opened it with data in it"


def _there(path: Path) -> bool:
    """Whether the file is there. A folder that cannot be read is unavailable, not missing: unknown
    is not missing (D-340), and `Path.exists` answers it differently from one Python to the next."""
    try:
        os.stat(path)
    except (FileNotFoundError, NotADirectoryError):
        return False
    except OSError as e:
        raise unavailable(path, e) from None
    return True


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
    """The engine on the data dir's database - or exit with one line naming the fix. A brand-new
    database is built first; one that has tables is never migrated here (D-324). Behind head it
    comes back with nothing written and no snapshot: the caller must put `call_check` before
    every read and write (D-338). `study migrate` takes its own copy and makes the user."""
    path = db_path()
    # Read before the tables: another first start takes its copy only once its tables are saved,
    # so a copy seen here is never from a build this start still sees as empty.
    had_data = has_snapshot()
    try:
        if had_data and not _there(path):
            # before opening it, which would leave an empty file behind
            _refuse(_missing(path, HAD_COPIES))
    except StudyError as e:
        _refuse(e)
    engine = make_engine(path)
    try:
        # No tables, not no file: a second program may have just created the file and still be
        # building in it. Both then run the upgrade; the write lock makes the later one wait, and
        # it finds nothing left to do.
        if not has_tables(engine):
            if had_data:
                _refuse(_missing(path, HAD_COPIES))
            upgrade(engine)
        check_schema(engine)
        _make_missing_user(engine)
    except SchemaBehindHead as e:
        logging.getLogger("studysystem").warning("%s: %s. %s", e.code, e.message, e.fix)
        return engine
    except SchemaAheadOfHead as e:
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


def call_check(engine: Engine) -> Callable[[], None]:
    """What both front doors run before every call: the database `open_database` opened is still
    there (D-339), and it is the one this build expects (D-338)."""
    path = db_path()
    at_head = schema_check(engine)

    def lost() -> StudyError:
        # On a Mac an open connection keeps a deleted file alive, and would go on writing to it
        # once the file is back.
        engine.dispose()
        return _missing(path, HAD_OPENED)

    def check() -> None:
        if not _there(path):  # before connecting, which would make an empty file
            raise lost()
        try:
            at_head()
        except SchemaBehindHead as e:
            if e.current is None and not has_tables(engine):
                raise lost() from None
            raise

    return check
