"""The generation contract, outbound (D-11, D-47): the host asks, the server hands it a task."""

from mcp.server import MCPServer
from mcp.types import CallToolResult
from sqlalchemy import Engine

from studysystem.services import generation
from studysystem.services.users import current_user
from studysystem.tools.results import run_task


def register(mcp: MCPServer, engine: Engine) -> None:
    @mcp.tool(
        name="study_start_generation",
        description=(
            "Start a generation task. Returns {task_id, content, schema, rules}: read content, "
            "follow rules, and send the result as JSON matching schema with "
            "study_submit_generation(task_id, payload). A content page with image: n comes with "
            "picture n after the JSON - the whole page, figures included; read the figures "
            "from it. kind: 'transcription' turns a past "
            "exam paper into its questions - pass past_exam_id, the id study_add_past_exam "
            "returned. Other kinds are not available yet. Calling again for a paper whose task "
            "is still open returns the same task, so a lost payload can be fetched again."
        ),
    )
    def study_start_generation(kind: str, past_exam_id: str | None = None) -> CallToolResult:
        return run_task(
            lambda: generation.start_generation(engine, current_user(engine), kind, past_exam_id)
        )
