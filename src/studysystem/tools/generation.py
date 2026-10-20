"""The generation contract (D-11, D-47): the host asks, the server hands it a task; the host
submits, the server keeps what is valid (D-52, D-53)."""

from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.types import CallToolResult
from pydantic import WithJsonSchema
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
            "its note is refused once its note task is no longer open. kind: 'tagging' links "
            "one course material to the course's "
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
            "from 1; skipped claims are already stored for the paper. A scanned paper's "
            "transcription reply adds pages_empty (pages no item names - check them) and "
            "read_back (each question beside its page, and page 1's header beside the registered "
            "paper): show it to the student, who confirms with study_confirm_transcription. "
            "A note's reply adds exports: where each note's markdown and PDF copies were "
            "written in the vault, or the problem and its fix - the note itself is saved "
            "either way, and study_export_notes writes what is missing once the problem is "
            "fixed."
        ),
    )
    # payload is Any, not dict: a wrong shape (a JSON string, a list) reaches the service's
    # envelope check and comes back as item_errors, not as the SDK's validation text (D-36).
    # The schema still tells the host it is an object - every built kind's answer is one, and a
    # task in parts is still submitted whole (D-145).
    def study_submit_generation(
        task_id: str, payload: Annotated[Any, WithJsonSchema({"type": "object"})]
    ) -> CallToolResult:
        return run(
            lambda: generation.submit_generation(engine, current_user(engine), task_id, payload)
        )

    @mcp.tool(
        name="study_confirm_transcription",
        description=(
            "Confirm a scanned paper's transcription after comparing the submit reply's "
            "read_back with the paper itself: each question beside its page, and the header "
            "read from page 1 beside the registered row. A scan counts toward the exam "
            "profile only once confirmed (D-210). Do not confirm a wrong one: it stays "
            "uncounted, and redoing it arrives with study_rescope_generation."
        ),
    )
    def study_confirm_transcription(past_exam_id: str) -> CallToolResult:
        return run(
            lambda: generation.confirm_transcription(engine, current_user(engine), past_exam_id)
        )
