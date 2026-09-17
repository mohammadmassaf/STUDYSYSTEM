"""The MCP front door: build the server, wire every tool module, run the transport."""

import datetime

from mcp.server import MCPServer

from studysystem.db.engine import db_path, make_engine, snapshot
from studysystem.tools import ping


def create_server() -> MCPServer:
    """Build a fully wired server. Called by __main__ and by tests."""
    mcp = MCPServer("studysystem")
    ping.register(mcp)
    return mcp


def main() -> None:
    engine = make_engine(db_path())
    snapshot(engine, datetime.datetime.now().astimezone().date())
    create_server().run()
