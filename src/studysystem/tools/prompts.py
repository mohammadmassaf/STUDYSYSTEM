"""MCP prompts (D-206, D-208, D-209): ready-made requests the student picks from the host's
menu. The host shows a form, one box per argument, and drops the filled-in text into the chat.

Both study flows fetch the plan before the session starts: an hour counts as plan use only when
it is logged on a topic or setup item shown in the plan in force when the session started
(D-113)."""

from typing import Annotated

from mcp.server import MCPServer
from pydantic import Field

# The last step of every flow: minutes asked for, scoped to what the plan showed.
LOG_STEP = (
    "When I say we are done, ask me how many minutes I studied - never estimate them - and log "
    "them with study_log_hours, passing the session_id if a session was started, and the "
    "item's topic_id or setup_key exactly as study_get_plan returned it. Pass neither only if "
    "no shown item fits, and say so."
)


def register(mcp: MCPServer) -> None:
    @mcp.prompt(
        name="study_plan",
        title="Make a new plan",
        description="Rank everything again from today's data; read the result on the dashboard.",
    )
    def study_plan() -> str:
        # The dashboard shows the latest saved plan and never makes one (D-163): this is the way
        # to refresh it outside a study flow.
        return (
            "Make me a new study plan: call study_get_plan once. Do not list its items here - "
            "I read them on the dashboard, which shows the same items. Reply in one line: the "
            "plan is made, how many items each lane shows, and that I should refresh the "
            "dashboard."
        )

    @mcp.prompt(
        name="study_day",
        title="Start a study day",
        description="Get today's plan, pick an item, work on it, then log the minutes on it.",
    )
    def study_day() -> str:
        return (
            "Start my study day, using the studysystem tools:\n"
            "1. Call study_get_plan and show me every item it returns, lane by lane, with its "
            "reason. Ask me which one I take.\n"
            "2. A review or study item: before any work, start a session on it with "
            "study_start_session - review -> mode 'review-quiz', study -> mode 'practice', both "
            "with subject_type 'topic' and subject_id the item's topic_id. A setup item: start "
            "no session; say what it asks for and do it with me. A first-material item is the "
            "exception: stop here and tell me to run the 'Study a chapter' prompt from the menu "
            "- it gets the plan again, adds the chapter, makes its note, explains it and logs "
            "the minutes on this item.\n"
            "3. Do the work with me.\n"
            f"4. {LOG_STEP}"
        )

    @mcp.prompt(
        name="study_chapter",
        title="Study a chapter",
        description="Add one chapter PDF to a course, make its exam note, then explain it.",
    )
    def study_chapter(
        code: Annotated[str, Field(description="Course code, e.g. I3302")],
        path: Annotated[
            str, Field(description="Full path to the PDF (in Explorer: select it, Ctrl+Shift+C)")
        ],
    ) -> str:
        return (
            f"Course {code}. Study the chapter at {path} with me, using the studysystem tools:\n"
            "1. Call study_get_plan and show me every item it returns, lane by lane. Keep the "
            "one this chapter serves: a first-material setup item for this course, or a study "
            "or review topic the chapter teaches - ask me to confirm it.\n"
            "2. Add the chapter with study_add_material (kind 'chapter' unless it is clearly "
            "slides).\n"
            "3. Make its exam note: study_start_generation kind 'note' with the material_id, "
            "fetch every part, then study_submit_generation until it reports no errors (at most "
            "3 submissions). If the material already existed and its note is refused as done, "
            "skip to step 4.\n"
            "4. Explain it: study_start_session mode 'explain-chapter', subject_type 'material', "
            "subject_id the material_id from step 2. Then teach me the chapter in the order of "
            "its own pages, going deeper where the note marks exam targets. Stop after each "
            "section so I can ask questions.\n"
            f"5. {LOG_STEP}"
        )
