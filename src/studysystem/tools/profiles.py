"""Exam profiles (D-67 - D-69, D-156, D-167, D-169): derive a slot's topic weights, the claims
it keeps and how provisional each class is; and the instructor's statements that outrank the
papers (D-172, D-173)."""

from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.types import CallToolResult
from pydantic import WithJsonSchema
from sqlalchemy import Engine

from studysystem.services import profiles, statements
from studysystem.services.users import current_user
from studysystem.tools.results import run


def register(mcp: MCPServer, engine: Engine) -> None:
    @mcp.tool(
        name="study_derive_profile",
        description=(
            "Derive the next version of an assessment's exam profile from its transcribed past "
            "papers, named by course code and assessment name (e.g. 'Final exam'): each topic's "
            "weight as a percent, newer papers counting more. Needs at least 3 transcribed "
            "papers (a scan counts once confirmed with study_confirm_transcription), and every "
            "proposed topic on their items accepted or declined first "
            "(study_confirm_topic_proposal). Call it once those topics are confirmed, and again "
            "after a new paper's topics are. Open generation tasks built on the older profile "
            "are invalidated - start them again. It also keeps one paper's format claims (the "
            "newest paper of the session the student sits next, or a newer paper of the other "
            "session when that one is more than 5 years old) and the instructor-driven claims of "
            "papers by the course's instructor; claims counts them per class. provisional "
            "states, per class, whether the evidence is thin and why - tell the student. Derive "
            "again after the course's instructor changes, or after an instructor statement "
            "(study_add_instructor_statement): a statement still counting outranks the papers - "
            "weights[].source is 'instructor' for a topic whose share the instructor stated, "
            "and statements lists the ones used (statements_set_aside: older shares that no "
            "longer fit in 100% - tell the student). semester_name is needed only if the code "
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

    @mcp.tool(
        name="study_add_instructor_statement",
        description=(
            "Store one thing the instructor said about an assessment's coming exam - only when "
            "the student reports it; never infer one from a past paper. One claim per call: "
            'claim_key \'topic-marks\' with claim_value {"marks": 30, "of_total": 100} and '
            "topic_id (a confirmed topic's id from study_get_topics) for \"chapter X is 30% of "
            'the final" - marks 0 for "X is not on it"; or a format fact with no topic: '
            "'duration-minutes' {\"minutes\": 120}, 'total-marks' {\"marks\": 60}, "
            "'question-count' {\"count\": 5}, 'has-mcq' {\"value\": true}. citation: where or "
            "when it was said, e.g. 'lecture 2026-10-16'. A statement outranks what the past "
            "papers imply until a paper dated after it is added; a newer statement on the same "
            "key (and topic) replaces it. A statement cannot be taken back, only replaced by a "
            "newer one - confirm it with the student before calling. Refused with "
            "pins_over_100 when the stated shares "
            "would pass 100%. Call study_derive_profile afterwards so the profile uses it, and "
            "repeat stated to the student. semester_name is needed only if the code exists in "
            "two semesters."
        ),
    )
    # claim_value is Any, not dict: a wrong shape reaches the service's check and comes back
    # as a structured invalid_value, not the SDK's validation text (D-36).
    def study_add_instructor_statement(
        code: str,
        assessment: str,
        claim_key: str,
        claim_value: Annotated[Any, WithJsonSchema({"type": "object"})],
        topic_id: str | None = None,
        citation: str | None = None,
        semester_name: str | None = None,
    ) -> CallToolResult:
        return run(
            lambda: statements.add_instructor_statement(
                engine,
                current_user(engine),
                code,
                assessment,
                claim_key,
                claim_value,
                topic_id,
                citation,
                semester_name,
            )
        )
