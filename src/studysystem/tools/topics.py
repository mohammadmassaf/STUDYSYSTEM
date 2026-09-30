"""Topics (D-13, D-60 - D-66): list a course's topics with their items, then accept or decline
each proposal."""

from mcp.server import MCPServer
from mcp.types import CallToolResult
from sqlalchemy import Engine

from studysystem.services import topics
from studysystem.services.users import current_user
from studysystem.tools.results import run


def register(mcp: MCPServer, engine: Engine) -> None:
    @mcp.tool(
        name="study_get_topics",
        description=(
            "List a course's live topics (proposed, active, unexamined), named by course code, "
            "each with its status and its items: item id, paper_date, position, marks and the "
            "full question text, in paper order. Read-only. Call it before "
            "study_confirm_topic_proposal: show the student each proposed topic with its "
            "questions, and use the item ids here to build a decline's retag. semester_name "
            "is needed only if the code exists in two semesters."
        ),
    )
    def study_get_topics(code: str, semester_name: str | None = None) -> CallToolResult:
        return run(lambda: topics.get_topics(engine, current_user(engine), code, semester_name))

    @mcp.tool(
        name="study_confirm_topic_proposal",
        description=(
            "Record the student's decision on one proposed topic. decision 'accept': the topic "
            "becomes active and its items stay; send no retag. decision 'decline': the topic "
            "is retired and every one of its items moves - retag must list each item of the "
            "topic exactly once, as {item_id, topic: {id}} for an existing live topic or "
            "{item_id, topic: {proposed_name}} for a topic by name (matched ignoring case; a "
            "new name becomes a new proposed topic, to accept later). Items may go to "
            "different targets, never to the declined topic itself. Only the student decides: "
            "never accept or decline on your own."
        ),
    )
    def study_confirm_topic_proposal(
        topic_id: str, decision: str, retag: list[dict] | None = None
    ) -> CallToolResult:
        return run(
            lambda: topics.confirm_topic_proposal(
                engine, current_user(engine), topic_id, decision, retag
            )
        )
