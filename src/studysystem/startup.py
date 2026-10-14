"""What every front door runs before it serves: the MCP server (`study serve`) and the dashboard
route (`study dashboard`) open the database the same way and refuse it for the same reasons -
behind head, ahead of head, locked, damaged (D-22, D-36) - then take the day's snapshot."""

import datetime
import logging
import sys
from typing import NoReturn

from sqlalchemy import Engine
from sqlalchemy.exc import DatabaseError

from studysystem.db.engine import WalUnavailable, db_path, make_engine, snapshot, unavailable
from studysystem.db.migrate import (
    SchemaAheadOfHead,
    SchemaBehindHead,
    check_schema,
    head_revision,
)
from studysystem.errors import StudyError


def _refuse(e: StudyError) -> NoReturn:
    print(f"{e.code}: {e.message}. {e.fix}", file=sys.stderr)
    sys.exit(1)


def open_database() -> Engine:
    """The engine on the data dir's database, at head - or exit with one line naming the fix."""
    path = db_path()
    if not path.exists():
        # Opening it would leave an empty file behind; the refusal is the empty database's.
        _refuse(SchemaBehindHead(None, head_revision()))
    engine = make_engine(path)
    try:
        check_schema(engine)
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
