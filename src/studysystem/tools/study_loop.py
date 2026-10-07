"""The study loop (D-17, 1.16): start a session, submit a graded problem, log the minutes.

Sessions label the work and never hold minutes; hours are always the student's explicit
assertion. Each tool is one line of work through `run`, so a bad value comes back in the D-17
error shape.
"""

from mcp.server import MCPServer
from mcp.types import CallToolResult
from sqlalchemy import Engine

from studysystem.services import study_loop
from studysystem.services.users import current_user
from studysystem.tools.results import run


def register(mcp: MCPServer, engine: Engine) -> None:
    @mcp.tool(
        name="study_start_session",
        description=(
            "Start a study session on a course, before the work begins. mode: 'practice' "
            "(questions solved alone - the usual evening), 'exam-walkthrough' (a past paper "
            "explained question by question), 'explain-chapter', 'mock' (a paper sat cold) or "
            "'review-quiz' (due reviews). Optionally name what it is about: subject_type "
            "'topic', 'material' or 'past-exam' with subject_id from study_get_topics. A "
            "session records no time - log the minutes with study_log_hours when done; one "
            "left open expires after 4 hours."
        ),
    )
    def study_start_session(
        code: str,
        mode: str,
        subject_type: str | None = None,
        subject_id: str | None = None,
        semester_name: str | None = None,
    ) -> CallToolResult:
        return run(
            lambda: study_loop.start_session(
                engine, current_user(engine), code, mode, subject_type, subject_id, semester_name
            )
        )

    @mcp.tool(
        name="study_submit_attempt",
        description=(
            "Record one problem the student solved, once it is graded: all its parts in one "
            "call. Grade each part against the question, show the student the grades and let "
            "them correct any, then submit. Each attempt: practice_item_id (from "
            "study_get_topics), or topic_id for a question with no item; score 0-1 for a part "
            "partly right, correct true/false otherwise (score wins when both are sent); "
            "optional root_cause ('concept', 'recall', 'procedure', 'misread', 'careless', "
            "'time-pressure') and fix_rule (the student's own rule for next time). Goes to the "
            "session named, else the course's open session, else a new practice session. "
            "Returns each topic's next review date."
        ),
    )
    def study_submit_attempt(
        code: str,
        attempts: list[dict],
        session_id: str | None = None,
        semester_name: str | None = None,
    ) -> CallToolResult:
        return run(
            lambda: study_loop.submit_attempts(
                engine, current_user(engine), code, attempts, session_id, semester_name
            )
        )

    @mcp.tool(
        name="study_log_hours",
        description=(
            "Log the minutes the student says they studied a course (1-720) - the only way "
            "time is recorded; ask, never estimate. Scope them to a topic_id, or to a setup "
            "item's setup_key exactly as study_get_plan returned it (that item then leaves the "
            "plan for 7 days), or neither for course-level time - never both. Pass the "
            "session_id to close that session. To fix a mistyped entry, send voids with its "
            "hours_entry_id plus the correct minutes; send voids alone to cancel it."
        ),
    )
    def study_log_hours(
        code: str,
        minutes: str | float | None = None,
        topic_id: str | None = None,
        setup_key: str | None = None,
        session_id: str | None = None,
        voids: str | None = None,
        semester_name: str | None = None,
    ) -> CallToolResult:
        return run(
            lambda: study_loop.log_hours(
                engine,
                current_user(engine),
                code,
                minutes,
                topic_id,
                setup_key,
                session_id,
                voids,
                semester_name,
            )
        )
