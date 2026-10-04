"""The MCP front door: build the server, wire every tool module, run the transport."""

import datetime
import logging
import sys

from mcp.server import MCPServer
from sqlalchemy import Engine

from studysystem.db.engine import db_path, make_engine, snapshot
from studysystem.db.migrate import SchemaBehindHead, check_schema
from studysystem.tools import (
    course_setup,
    generation,
    materials,
    notes,
    past_exams,
    ping,
    profiles,
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
    return mcp


def main() -> None:
    # stderr only: stdout carries the MCP messages. The SDK stays at warnings; ours at info.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(asctime)s %(message)s")
    logging.getLogger("studysystem").setLevel(logging.INFO)
    engine = make_engine(db_path())
    try:
        check_schema(engine)
    except SchemaBehindHead as e:
        print(f"{e.code}: {e.message}. {e.fix}", file=sys.stderr)
        sys.exit(1)
    snapshot(engine, datetime.datetime.now().astimezone().date())
    create_server(engine).run()
