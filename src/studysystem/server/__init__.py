"""The MCP front door: build the server, wire every tool module, run the transport."""

import logging
import sys
from collections.abc import Callable
from typing import Any

from mcp.server import MCPServer
from sqlalchemy import Engine
from starlette.concurrency import run_in_threadpool

from studysystem.services import values
from studysystem.startup import call_check, open_database
from studysystem.tools import (
    course_setup,
    dashboard,
    feedback,
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
from studysystem.tools.results import refusal

# It never opens the database, and the page it opens shows why the other tools refuse (D-338).
UNCHECKED_TOOLS = {"study_dashboard"}


class CheckedServer(MCPServer):
    """Runs `check` before every tool, in the one place every call passes - so a new tool cannot
    forget it (D-338)."""

    def __init__(self, name: str, check: Callable[[], None] | None):
        super().__init__(name)
        self._check = check

    async def call_tool(self, name: str, arguments: dict[str, Any], context=None):
        refused = refusal(lambda: values.storable("arguments", arguments))
        if refused is not None:
            return refused
        if self._check is not None and name not in UNCHECKED_TOOLS:
            # In a worker thread, as the SDK runs the tools: the check reads the database.
            refused = await run_in_threadpool(refusal, self._check)
            if refused is not None:
                return refused
        return await super().call_tool(name, arguments, context)


def create_server(engine: Engine, check: Callable[[], None] | None = None) -> MCPServer:
    """Build a fully wired server on `engine`. Called by main(), which passes `call_check`, and
    by tests, which pass their own engine - most of them made by `create_all`, with no revision to
    check."""
    mcp = CheckedServer("studysystem", check)
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
    dashboard.register(mcp)
    return mcp


def main() -> None:
    # stderr only: stdout carries the MCP messages. The SDK stays at warnings; ours at info.
    logging.basicConfig(stream=sys.stderr, level=logging.WARNING, format="%(asctime)s %(message)s")
    logging.getLogger("studysystem").setLevel(logging.INFO)
    engine = open_database()
    create_server(engine, call_check(engine)).run()
