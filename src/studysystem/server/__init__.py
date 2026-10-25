"""The MCP front door: build the server, wire every tool module, run the transport."""

import logging
import sys

from mcp.server import MCPServer
from sqlalchemy import Engine

from studysystem.startup import open_database
from studysystem.tools import (
    course_setup,
    feedback,
    gate,
    generation,
    materials,
    notes,
    past_exams,
    ping,
    plan,
    profiles,
    prompts,
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
    feedback.register(mcp, engine)
    plan.register(mcp, engine)
    study_loop.register(mcp, engine)
    gate.register(mcp, engine)
    summaries.register(mcp, engine)
    prompts.register(mcp)
    return mcp


def main() -> None:
    # stderr only: stdout carries the MCP messages. The SDK stays at warnings; ours at info.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(asctime)s %(message)s")
    logging.getLogger("studysystem").setLevel(logging.INFO)
    create_server(open_database()).run()
