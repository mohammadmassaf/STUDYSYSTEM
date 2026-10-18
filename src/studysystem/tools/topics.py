"""Topics (D-13, D-60 - D-66): list a course's topics with their items, accept or decline each
proposal, and split or merge an active topic (2.11, D-192 - D-197, D-200, D-201, D-203)."""

from typing import Annotated, Any

from mcp.server import MCPServer
from mcp.types import CallToolResult
from pydantic import WithJsonSchema
from sqlalchemy import Engine

from studysystem.services import taxonomy, topics
from studysystem.services.users import current_user
from studysystem.tools.results import run


def register(mcp: MCPServer, engine: Engine) -> None:
    @mcp.tool(
        name="study_get_topics",
        description=(
            "List a course's live topics (proposed, active, unexamined), named by course code, "
            "each with its status and its items: item id, paper_date, position, marks, the "
            "full question text and best_score, in paper order. best_score is the best "
            "unassisted result on the item (null = never practised); an item scored above 0.5 "
            "is not listed - it is not handed out again - and `hidden` counts them per topic; "
            "include_hidden true lists those too (hidden is then 0). "
            "Read-only. Call it before "
            "study_confirm_topic_proposal: show the student each proposed topic with its "
            "questions, and use the item ids here to build a decline's retag. Before "
            "study_split_topic, call it with include_hidden true: a split maps every item. "
            "semester_name is needed only if the code exists in two semesters."
        ),
    )
    def study_get_topics(
        code: str, semester_name: str | None = None, include_hidden: bool = False
    ) -> CallToolResult:
        return run(
            lambda: topics.get_topics(
                engine, current_user(engine), code, semester_name, include_hidden
            )
        )

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
    # retag is Any, not list[dict]: the service checks its shape, in the D-17 shape (D-36). The
    # schema still tells the host it is a list - WithJsonSchema changes the hint, not the check.
    def study_confirm_topic_proposal(
        topic_id: str,
        decision: str,
        retag: Annotated[
            Any,
            WithJsonSchema(
                {"anyOf": [{"type": "array", "items": {"type": "object"}}, {"type": "null"}]}
            ),
        ] = None,
    ) -> CallToolResult:
        return run(
            lambda: topics.confirm_topic_proposal(
                engine, current_user(engine), topic_id, decision, retag
            )
        )

    @mcp.tool(
        name="study_split_topic",
        description=(
            "Split one active or unexamined topic into two or more new topics (D-13): the old "
            "one is retired and keeps its history, its review schedule is copied to each new "
            "topic as an estimate. children is [{name, item_ids}]: each new topic's name (not "
            "a live topic's name: to fold into an active topic, use study_merge_topics; a "
            "proposed one is decided first) and "
            "its share of the topic's items, every item study_get_topics lists for it with "
            "include_hidden true named exactly once (mastered ones too), each new topic at "
            "least one. Only the student decides which "
            "question goes where: show the questions and ask. The result's derive_again names "
            "each exam whose profile still weights the old topic: call study_derive_profile "
            "for it once the student is done fixing topics; retag_materials lists the "
            "chapters each new topic was linked to, to re-tag if one does not belong."
        ),
    )
    # children is Any, not list[dict]: the service checks its shape (D-36), as retag above.
    def study_split_topic(
        topic_id: str,
        children: Annotated[Any, WithJsonSchema({"type": "array", "items": {"type": "object"}})],
    ) -> CallToolResult:
        return run(lambda: taxonomy.split_topic(engine, current_user(engine), topic_id, children))

    @mcp.tool(
        name="study_merge_topics",
        description=(
            "Fold one active or unexamined topic (source_id) into another of the same course "
            "(target_id), when the two name one competence (D-13): every item and attempt of "
            "the source moves to the target, the source is retired pointing at it, and the "
            "target's review schedule is rebuilt from both topics' attempts. Only the student "
            "decides: never merge on your own. The result's derive_again names each exam whose "
            "profile still weights the source: call study_derive_profile for it once the "
            "student is done fixing topics."
        ),
    )
    def study_merge_topics(source_id: str, target_id: str) -> CallToolResult:
        return run(
            lambda: taxonomy.merge_topics(engine, current_user(engine), source_id, target_id)
        )
