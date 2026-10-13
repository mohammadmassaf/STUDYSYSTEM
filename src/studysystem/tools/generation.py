"""The generation contract (D-11, D-47): the host asks, the server hands it a task; the host
submits, the server keeps what is valid (D-52, D-53)."""

from typing import Any

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
            "Start a generation task. Returns {task_id, part, parts, content, schema, rules}: "
            "read content, follow rules, and send the result as JSON matching schema with "
            "study_submit_generation(task_id, payload). A task too big for one reply comes in "
            "parts: when parts is more than 1, call again with the same kind and id and "
            "part=2, then 3... up to parts, in order, and read them all before writing. Each "
            "later part carries only its pages and its share of the rules' long list (topics, "
            "or past_exam_items) - join them to part 1's. Submit is refused until every part "
            "was fetched; task_changed means the content moved since part 1: start again at "
            "part 1. A content page with image: n comes with "
            "picture n after the JSON - the whole page, figures included, numbered within this "
            "part; read the figures "
            "from it. kind: 'transcription' turns a past "
            "exam paper into its questions - pass past_exam_id, the id study_add_past_exam "
            "returned. kind: 'note' turns one course material into an exam-ready study note - "
            "pass material_id, the id study_add_material returned; a material that already has "
            "its note is refused. kind: 'tagging' links one course material to the course's "
            "topics it teaches - pass material_id; it can be run again on the same material, "
            "and a course with no active topic yet is refused. kind: 'observation' records "
            "what one past exam paper shows about the exam - its duration, marks, question "
            "count, and the instructor's archetypes, phrasing and traps - pass past_exam_id; a "
            "paper that already holds observations is refused. Other kinds are not available "
            "yet. Calling again while a "
            "task is still open returns the same task, so a lost payload can be fetched again."
        ),
    )
    def study_start_generation(
        kind: str,
        past_exam_id: str | None = None,
        material_id: str | None = None,
        part: str | float = 1,  # loose: a bad value gets the structured error (D-36)
    ) -> CallToolResult:
        return run_task(
            lambda: generation.start_generation(
                engine, current_user(engine), kind, past_exam_id, material_id, part
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
            "A task in parts is refused with parts_missing until every part was fetched - that "
            "refusal does not count as a submission. "
            "A task takes at most 3 submissions; it closes when one has no errors and no "
            "missing position. A tagging's accepted and skipped list topic ids: accepted are "
            "newly linked, skipped were already linked to this material. An observation's "
            "accepted, skipped and item_errors name claims by their place in this payload, "
            "from 1; skipped claims are already stored for the paper. A note's reply adds "
            "exports: where each note's markdown and PDF copies were written in the vault, or "
            "the problem and its fix - the note itself is saved either way, and "
            "study_export_notes writes what is missing once the problem is fixed."
        ),
    )
    # payload is Any, not dict: a wrong shape (a JSON string, a list) reaches the service's
    # envelope check and comes back as item_errors, not as the SDK's validation text (D-36).
    def study_submit_generation(task_id: str, payload: Any) -> CallToolResult:
        return run(
            lambda: generation.submit_generation(engine, current_user(engine), task_id, payload)
        )
