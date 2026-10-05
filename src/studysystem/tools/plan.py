"""The scheduler (D-15, D-87, D-92): what to do next, across every course, in three lanes."""

import datetime

from mcp.server import MCPServer
from mcp.types import CallToolResult
from sqlalchemy import Engine

from studysystem.services import plan
from studysystem.services.users import current_user
from studysystem.tools.results import run


def today() -> datetime.date:
    """The student's local date: an exam's days-to-go count in Beirut, whatever the UTC date is."""
    return datetime.datetime.now().astimezone().date()


def register(mcp: MCPServer, engine: Engine) -> None:
    @mcp.tool(
        name="study_get_plan",
        description=(
            "Rank what to study next across every course, in three lanes: due reviews, topics "
            "to study (ranked by exam weight x weakness / days to the exam / hours), and setup "
            "items - inputs or materials worth getting, ranked by the marks they could unlock. "
            "Each item carries the reason it was picked, naming every guessed input. Returns "
            "the top shown_n of each lane plus how many more each lane holds; show the student "
            "every item returned, since the plan records them as shown. Every call saves a new "
            "plan."
        ),
    )
    def study_get_plan(shown_n: int = plan.SHOWN_N) -> CallToolResult:
        return run(lambda: plan.get_plan(engine, current_user(engine), today(), shown_n))
