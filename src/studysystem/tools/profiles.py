"""Exam profiles (D-67 - D-69, D-156, D-167, D-169): derive a slot's topic weights, the claims
it keeps and how provisional each class is."""

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
            "are invalidated - start them again. It also keeps one paper's format claims (the "
            "newest paper of the session the student sits next, or a newer paper of the other "
            "session when that one is more than 5 years old) and the instructor-driven claims of "
            "papers by the course's instructor; claims counts them per class. provisional "
            "states, per class, whether the evidence is thin and why - tell the student. Derive "
            "again after the course's instructor changes. semester_name is needed only if the "
            "code exists in two semesters."
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
