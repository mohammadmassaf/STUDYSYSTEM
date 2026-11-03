"""MCP prompts (D-206, D-208, D-209, D-234): ready-made requests the student picks from the
host's menu. The host shows a form, one box per argument, and drops the filled-in text into the
chat.

Every study flow fetches the plan first, before any session starts: an hour counts as plan use
only when it is logged on a topic or setup item shown in the plan in force when the session
started (D-113)."""

from typing import Annotated

from mcp.server import MCPServer
from pydantic import Field

# The first step of both chapter flows: the plan item the chapter's minutes will be logged on.
PLAN_STEP = (
    "Call study_get_plan and show me every item it returns, lane by lane. Keep the one this "
    "chapter serves: a first-material setup item for this course, or a study or review topic "
    "the chapter teaches - ask me to confirm it."
)

# Making a chapter's note: both chapter flows try it; a note already saved is refused (D-234).
MAKE_NOTE_STEP = (
    "Make its exam note: study_start_generation kind 'note' with the material_id, fetch every "
    "part, then study_submit_generation until it reports no errors (at most 3 submissions)."
)

# The last step of every flow: minutes asked for, scoped to what the plan showed.
LOG_STEP = (
    "When I say we are done, ask me how many minutes I studied - never estimate them - and log "
    "them with study_log_hours. Always pass the session_id if a session was started, even when "
    "no item fits. Also pass the item's topic_id or setup_key exactly as study_get_plan "
    "returned it; leave out both of those only if no shown item fits, and say so. If I say I "
    "spent no time, log nothing."
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
            "exception: stop here and tell me to run the 'Add a chapter' prompt from the menu - "
            "it gets the plan again, adds the chapter, makes its note and logs the minutes on "
            "this item. 'Explain a chapter' then teaches it.\n"
            "3. Do the work with me.\n"
            f"4. {LOG_STEP}"
        )

    @mcp.prompt(
        name="study_add_chapter",
        title="Add a chapter",
        description="Add one chapter PDF to a course and make its exam note. No teaching.",
    )
    def study_add_chapter(
        code: Annotated[str, Field(description="Course code, e.g. I3302")],
        path: Annotated[
            str, Field(description="Full path to the PDF (in Explorer: select it, Ctrl+Shift+C)")
        ],
    ) -> str:
        return (
            f"Course {code}. Add the chapter at {path} to the course, using the studysystem "
            "tools:\n"
            f"1. {PLAN_STEP}\n"
            "2. Add the chapter with study_add_material (kind 'chapter' unless it is clearly "
            "slides).\n"
            f"3. {MAKE_NOTE_STEP} If the material already existed and its note is refused as "
            "done, say so - the chapter is ready to explain.\n"
            "4. Do not teach the chapter here. Tell me to run the 'Explain a chapter' prompt "
            "when I want it taught.\n"
            f"5. {LOG_STEP}"
        )

    @mcp.prompt(
        name="study_explain_chapter",
        title="Explain a chapter",
        description=(
            "Teach one chapter with examples and visuals, section by section. Adds the chapter "
            "and makes its note first if either is missing."
        ),
    )
    def study_explain_chapter(
        code: Annotated[str, Field(description="Course code, e.g. I3302")],
        path: Annotated[
            str, Field(description="Full path to the PDF (in Explorer: select it, Ctrl+Shift+C)")
        ],
        note: Annotated[
            str | None,
            Field(
                description="Full path to the chapter's note (.md), if it was made in an "
                "earlier chat. Leave empty for a new chapter."
            ),
        ] = None,
    ) -> str:
        # No tool returns a note's text, so a note made in an earlier chat is read from its file.
        note_line = f"Its exam note is at {note} - read it before you teach.\n" if note else ""
        return (
            f"Course {code}. Explain the chapter at {path} to me, using the studysystem tools.\n"
            f"{note_line}"
            f"1. {PLAN_STEP}\n"
            "2. Call study_add_material with the path (kind 'chapter' unless it is clearly "
            "slides) to get the material_id. A chapter already added writes nothing and comes "
            "back existing: true.\n"
            f"3. Make sure it has a note. {MAKE_NOTE_STEP} If it is refused with already_noted, "
            "the note was made in an earlier chat: read it from the note path, or ask me for "
            "the path if I gave none.\n"
            "4. Start the session: study_start_session mode 'explain-chapter', subject_type "
            "'material', subject_id the material_id from step 2.\n"
            "5. Teach me the chapter section by section, in the order of its own pages or "
            "slides. A section is one idea, not one slide. For each section:\n"
            "   - Start from a concrete example, then name the idea it shows.\n"
            "   - Show it visually: a drawn diagram (an SVG or Mermaid picture, not boxes made "
            "of text characters), a table, or a run traced step by step.\n"
            "   - Keep the words short: at most 2-3 sentences between examples and visuals. No "
            "long paragraphs, and no bullet summary of a slide.\n"
            "   - Where the note marks an exam target, go deeper and show how the exam asks "
            "it.\n"
            "   - End with one short question that checks I understood, then stop and wait for "
            "me.\n"
            f"6. {LOG_STEP}"
        )
