"""Exam profiles (D-67 - D-69): derive a slot's topic weights from its transcribed papers."""

from mcp.server import MCPServer
from mcp.types import CallToolResult
from sqlalchemy import Engine

from studysystem.services import profiles
from studysystem.services.users import current_user
from studysystem.tools.results import run


def register(mcp: MCPServer, engine: Engine) -> None:
    @mcp.tool(
        name="study_derive_profile",
        description=(
            "Derive the next version of an assessment's exam profile from its transcribed past "
            "papers, named by course code and assessment name (e.g. 'Final exam'): each topic's "
            "weight as a percent, newer papers counting more. Needs at least 3 transcribed "
            "papers, and every proposed topic on their items accepted or declined first "
            "(study_confirm_topic_proposal). Call it once those topics are confirmed, and again "
            "after a new paper's topics are. Open generation tasks built on the older profile "
            "are invalidated - start them again. semester_name is needed only if the code "
            "exists in two semesters."
        ),
    )
    def study_derive_profile(
        code: str, assessment: str, semester_name: str | None = None
    ) -> CallToolResult:
        return run(
            lambda: profiles.derive_profile(
                engine, current_user(engine), code, assessment, semester_name
            )
        )
