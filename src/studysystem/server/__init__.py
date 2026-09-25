"""The MCP front door: build the server, wire every tool module, run the transport."""

import datetime
import sys

from mcp.server import MCPServer
from sqlalchemy import Engine

from studysystem.db.engine import db_path, make_engine, snapshot
from studysystem.db.migrate import SchemaBehindHead, check_schema
from studysystem.tools import course_setup, ping


def create_server(engine: Engine) -> MCPServer:
    """Build a fully wired server on `engine`. Called by main() and by tests, which pass their own
    engine."""
    mcp = MCPServer("studysystem")
    ping.register(mcp)
    course_setup.register(mcp, engine)
    return mcp


def main() -> None:
    engine = make_engine(db_path())
    try:
        check_schema(engine)
    except SchemaBehindHead as e:
        print(f"{e.code}: {e.message}. {e.fix}", file=sys.stderr)
        sys.exit(1)
    snapshot(engine, datetime.datetime.now().astimezone().date())
    create_server(engine).run()
