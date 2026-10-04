"""The generation contract (D-11, D-47): the host asks, the server hands it a task; the host
submits, the server keeps what is valid (D-52, D-53)."""

from mcp.server import MCPServer
from mcp.types import CallToolResult
from sqlalchemy import Engine

from studysystem.services import generation
from studysystem.services.users import current_user
from studysystem.tools.results import run, run_task


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
            "returned. kind: 'note' turns one course material into an exam-ready study note - "
            "pass material_id, the id study_add_material returned; a material that already has "
            "its note is refused. Other kinds are not available yet. Calling again while a "
            "task is still open returns the same task, so a lost payload can be fetched again."
        ),
    )
    def study_start_generation(
        kind: str, past_exam_id: str | None = None, material_id: str | None = None
    ) -> CallToolResult:
        return run_task(
            lambda: generation.start_generation(
                engine, current_user(engine), kind, past_exam_id, material_id
            )
        )

    @mcp.tool(
        name="study_submit_generation",
        description=(
            "Submit the result of a generation task: task_id from study_start_generation, "
            "payload the JSON its schema describes. Valid items are kept; each invalid one "
            "comes back in item_errors, named by its place in this payload - fix those and "
            "submit again, sending only them or everything (items already accepted are "
            "skipped, never rewritten). missing lists positions no item has filled yet. "
            "A task takes at most 3 submissions; it closes when one has no errors and no "
            "missing position. A note's reply adds exports: where each note's markdown and "
            "PDF copies were written in the vault, or the problem and its fix - the note "
            "itself is saved either way, and study_export_notes writes what is missing once "
            "the problem is fixed."
        ),
    )
    def study_submit_generation(task_id: str, payload: dict) -> CallToolResult:
        return run(
            lambda: generation.submit_generation(engine, current_user(engine), task_id, payload)
        )
