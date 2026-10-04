"""The note export, run again (D-79, task 1.14b): one tool that writes whatever a course's notes
are missing in the vault - the md, the PDF or both."""

from mcp.server import MCPServer
from mcp.types import CallToolResult
from sqlalchemy import Engine

from studysystem.services import notes
from studysystem.services.users import current_user
from studysystem.tools.results import run


def register(mcp: MCPServer, engine: Engine) -> None:
    @mcp.tool(
        name="study_export_notes",
        description=(
            "Write a course's notes into the vault wherever a file is missing: the markdown, "
            "the PDF copy beside it, or both. A note's export normally runs inside "
            "study_submit_generation; call this when that reply named a problem and it is "
            "fixed (vault folder declared, folder made writable, a file moved out of the way), "
            "or to give older notes their PDF. Safe to call twice - the second call writes "
            "nothing. Never overwrites a file. Course found by its code; semester_name only "
            "when the code exists in two semesters."
        ),
    )
    def study_export_notes(code: str, semester_name: str | None = None) -> CallToolResult:
        return run(
            lambda: notes.export_course_notes(engine, current_user(engine), code, semester_name)
        )
