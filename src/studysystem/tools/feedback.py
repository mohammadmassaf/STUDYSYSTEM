"""Feedback and directives (2.9; D-24, D-213): a rejection note on a bad item, the standing
instruction it may draft, and the student's yes that makes the instruction durable."""

from mcp.server import MCPServer
from mcp.types import CallToolResult
from sqlalchemy import Engine

from studysystem.services import feedback
from studysystem.services.users import current_user
from studysystem.tools.results import run


def register(mcp: MCPServer, engine: Engine) -> None:
    @mcp.tool(
        name="study_submit_feedback",
        description=(
            "File the student's feedback on generation. kind 'item-defect': a bad item - "
            "practice_item_id (from study_get_topics) and reason ('off-syllabus', 'wrong-topic', "
            "'duplicate', 'unclear', 'wrong-difficulty'; a wrong answer key is not taken yet), "
            "optional note in the student's words; the item leaves the live pool (a live or "
            "needs-review item, or a rejected one not yet filed). kind "
            "'format-preference' (how the student likes items) or 'generator-correction' (what "
            "the model keeps getting wrong): no item, needs directive_text and code (the course; "
            "not needed with scope 'all'). directive_text is one sentence future generation "
            "should follow; with an item defect it is optional, and it is a 'generator-correction' "
            "unless directive_kind is 'format-preference' (the student's own taste, e.g. 'I "
            "prefer short MCQ'). It is drafted as proposed and is used only after the student "
            "confirms it - read it back and ask, then call study_confirm_directive. scope: "
            "'topic' (the item's topic, the default for an item defect), 'course' (the default "
            "otherwise) or 'all' courses. A claim about the instructor ('the instructor writes "
            "easy questions') is not feedback - use study_add_instructor_statement. "
            "semester_name is needed only if the code exists in two semesters."
        ),
    )
    def study_submit_feedback(
        kind: str,
        practice_item_id: str | None = None,
        reason: str | None = None,
        note: str | None = None,
        directive_text: str | None = None,
        scope: str | None = None,
        code: str | None = None,
        semester_name: str | None = None,
        directive_kind: str | None = None,
    ) -> CallToolResult:
        return run(
            lambda: feedback.submit_feedback(
                engine,
                current_user(engine),
                kind,
                practice_item_id=practice_item_id,
                reason=reason,
                note=note,
                directive_text=directive_text,
                scope=scope,
                code=code,
                semester_name=semester_name,
                directive_kind=directive_kind,
            )
        )

    @mcp.tool(
        name="study_confirm_directive",
        description=(
            "Make a proposed directive active, on the student's explicit yes - never on your own "
            "judgement. directive_id is the id study_submit_feedback returned. From then on the "
            "directive travels with every generation task in its scope."
        ),
    )
    def study_confirm_directive(directive_id: str) -> CallToolResult:
        return run(lambda: feedback.confirm_directive(engine, current_user(engine), directive_id))
