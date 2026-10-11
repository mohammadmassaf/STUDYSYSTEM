"""The MCP front door: build the server, wire every tool module, run the transport."""

import datetime
import logging
import sys

from mcp.server import MCPServer
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
from studysystem.tools import (
    course_setup,
    gate,
    generation,
    materials,
    notes,
    past_exams,
    ping,
    plan,
    profiles,
    study_loop,
    summaries,
    topics,
)


def create_server(engine: Engine) -> MCPServer:
    """Build a fully wired server on `engine`. Called by main() and by tests, which pass their own
    engine."""
    mcp = MCPServer("studysystem")
    ping.register(mcp)
    course_setup.register(mcp, engine)
    past_exams.register(mcp, engine)
    materials.register(mcp, engine)
    notes.register(mcp, engine)
    generation.register(mcp, engine)
    topics.register(mcp, engine)
    profiles.register(mcp, engine)
    plan.register(mcp, engine)
    study_loop.register(mcp, engine)
    gate.register(mcp, engine)
    summaries.register(mcp, engine)
    return mcp


def _refuse(e: StudyError) -> None:
    print(f"{e.code}: {e.message}. {e.fix}", file=sys.stderr)
    sys.exit(1)


def main() -> None:
    # stderr only: stdout carries the MCP messages. The SDK stays at warnings; ours at info.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(asctime)s %(message)s")
    logging.getLogger("studysystem").setLevel(logging.INFO)
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
    create_server(engine).run()
