"""Material intake (D-05, D-44, task 1.14): one tool, one PDF per call."""

from mcp.server import MCPServer
from mcp.types import CallToolResult
from sqlalchemy import Engine

from studysystem.services import materials
from studysystem.services.users import current_user
from studysystem.tools.results import run


def register(mcp: MCPServer, engine: Engine) -> None:
    @mcp.tool(
        name="study_add_material",
        description=(
            "Register one course material - a chapter, slide deck or other PDF the student "
            "studies from - under a course found by its code. path: the local path of one PDF "
            "(page photos are not supported yet). kind: 'slides', 'chapter', 'lecture-notes', "
            "'textbook', 'exercises', 'syllabus' or 'other'. The server keeps its own copy of "
            "the file. The same PDF added again returns the material that already holds it, "
            "with existing: true, and writes nothing. Pass semester_name only when the code "
            "exists in more than one semester."
        ),
    )
    def study_add_material(
        code: str, path: str, kind: str, semester_name: str | None = None
    ) -> CallToolResult:
        return run(
            lambda: materials.add_material(
                engine, current_user(engine), code, path, kind, semester_name
            )
        )
