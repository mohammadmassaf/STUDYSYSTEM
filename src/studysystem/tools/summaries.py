"""Chapter summaries (1.20, D-131): two standalone tools - the host asks for a material's pages
and writes the summary, then hands the text back to be saved in the vault. No task, no row."""

from mcp.server import MCPServer
from mcp.types import CallToolResult
from sqlalchemy import Engine

from studysystem.services import summaries
from studysystem.services.users import current_user
from studysystem.tools.results import run, run_task


def register(mcp: MCPServer, engine: Engine) -> None:
    @mcp.tool(
        name="study_get_summary_task",
        description=(
            "Get what you need to write a short summary of one course material: returns "
            "{material_id, material, content, exam, rules}. Read content - a content page with "
            "image: n comes with picture n after the JSON, the whole page, figures included. "
            "exam is what the past papers ask of it: its topics' exam shares and past "
            "questions, the lines its note marked as exam targets, and its note's To-Memorize "
            "table - weigh the summary by these only. Follow rules.steps, then save the "
            "summary with study_save_summary. "
            "Use it when "
            "the student asks for a summary of a chapter; it is not the exam note (that is "
            "study_start_generation kind 'note'). Writes nothing, so it can be called again. "
            "material_id is the id study_add_material returned."
        ),
    )
    def study_get_summary_task(material_id: str) -> CallToolResult:
        return run_task(lambda: summaries.summary_task(engine, current_user(engine), material_id))

    @mcp.tool(
        name="study_save_summary",
        description=(
            "Save a summary written from study_get_summary_task into the vault: the markdown "
            "goes to the course's notes/ folder as <material>_summary.md, with a PDF copy "
            "beside it. Never overwrites - a name already taken gets (2), (3)... Returns the "
            "paths written; a PDF that failed comes back as problem and fix, the markdown "
            "being saved. An error means nothing was saved: fix it and send the same text "
            "again. Send the whole summary as markdown, with no frontmatter."
        ),
    )
    def study_save_summary(material_id: str, markdown: str) -> CallToolResult:
        return run(
            lambda: summaries.save_summary(engine, current_user(engine), material_id, markdown)
        )
