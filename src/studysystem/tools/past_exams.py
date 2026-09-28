"""Past-exam intake (D-17, D-42, D-44): one tool, one paper per call."""

from mcp.server import MCPServer
from mcp.types import CallToolResult
from sqlalchemy import Engine

from studysystem.services import past_exams
from studysystem.services.users import current_user
from studysystem.tools.results import run


def register(mcp: MCPServer, engine: Engine) -> None:
    @mcp.tool(
        name="study_add_past_exam",
        description=(
            "Register one past exam paper under one of a course's existing assessments, named "
            "by course code and assessment name (e.g. 'Final exam'). A resit paper goes under "
            "the same assessment with session_type 'second'. paths: the local path of one PDF, "
            "or of the paper's page images (.jpg, .png) in page order - all the photos of one "
            "paper go in one call. session_type: 'first' or 'second'. session_date: the exam "
            "day printed on the paper, YYYY-MM-DD; never guess it. instructor: the name printed "
            "on the paper; leave it out if none is printed. session_delayed: true only when "
            "that year's session ran late. The server keeps its own copy of the file; the same "
            "paper twice under one assessment is refused."
        ),
    )
    def study_add_past_exam(
        code: str,
        assessment: str,
        session_type: str,
        session_date: str,
        paths: list[str] | str,
        instructor: str | None = None,
        session_delayed: bool | str = False,
        semester_name: str | None = None,
    ) -> CallToolResult:
        return run(
            lambda: past_exams.add_past_exam(
                engine,
                current_user(engine),
                code,
                assessment,
                session_type,
                session_date,
                paths,
                instructor,
                session_delayed,
                semester_name,
            )
        )
