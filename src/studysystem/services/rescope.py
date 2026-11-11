"""Rescope (4.4; D-16 d, D-261, D-264): a set that rests on a wrong premise - a misread scan, a
practice set on the wrong chapter - is retired as `superseded`, never deleted, and a new task of
the same kind takes its place. Its items may already be in `topic_state`, so they stay as rows.

Who writes what: `study_rescope_generation` runs it all in one write unit - the new task first
(the old task's `superseded_by` points at it), then the old task and its items, then, for a
transcription, the paper and the exam edges its items backed.
"""

import json
import logging

from sqlalchemy import Connection, Engine, func, insert, select, update

from studysystem.db.tables import coverage, generation_task, past_exam, practice_item, topic
from studysystem.errors import StudyError
from studysystem.services import replacements, values
from studysystem.services.generation import _find_material, _whole_number
from studysystem.services.ids import new_id, now
from studysystem.services.topics import exam_assessments
from studysystem.services.units import write_unit

log = logging.getLogger(__name__)

# The kinds that hold a set of items; a note is redone with redo=true (D-250).
RESCOPABLE = ("transcription", "practice")


def rescope_generation(
    engine: Engine,
    user_id: str,
    task_id: str | None,
    material_id: str | None = None,
    count: object = None,
) -> dict:
    """Retire task `task_id` and its items as `superseded` and open a new task of the same kind
    in their place (D-16 d, D-264), in one write unit: the new task, then the old task and its
    items, then - for a transcription - the paper back to untranscribed and the exam edges no
    live question backs any more (D-261). A practice task may move to `material_id` and take a
    new `count`; what is not sent is copied from the old task. The pending replacements its old
    task left are dropped (D-265).

    The host then fetches the new task with study_start_generation on the same scope, which
    finds it open. Returns {"old_task_id", "new_task_id", "kind", "scope_type", "scope_id",
    "item_count", "superseded_items", "retired_edges", "dropped_replacements", "next"}."""
    if task_id is None:
        raise values.invalid("task_id", "is missing", "send the task_id of the set to retire")

    with write_unit(engine) as conn:
        old = conn.execute(
            select(generation_task).where(
                generation_task.c.id == task_id, generation_task.c.user_id == user_id
            )
        ).one_or_none()
        if old is None:
            raise StudyError(
                code="not_found",
                message=f"no generation task {task_id}",
                fix="use the task_id that study_start_generation returned",
                field_errors=[{"field": "task_id", "problem": "no such task"}],
            )
        scope = _rescope_scope(conn, user_id, old, material_id, count)

        new_task_id = new_id()  # first: the old task's superseded_by points at this row
        conn.execute(
            insert(generation_task).values(
                id=new_task_id,
                user_id=user_id,
                kind=old.kind,
                route="host",
                created_at=now(),
                exam_profile_id=None,  # neither kind is built on a profile
                **scope,
            )
        )
        topic_ids = _supersede(conn, old, new_task_id)
        superseded = conn.execute(
            select(func.count()).select_from(practice_item).where(practice_item.c.task_id == old.id)
        ).scalar_one()
        # its holes would be filled from the retired task's material (D-265)
        dropped = replacements.drop_from_task(conn, old.id)

        retired: list[str] = []
        if old.kind == "transcription":
            # the paper counts nowhere until the redo is submitted and confirmed (D-210, D-264)
            conn.execute(
                update(past_exam)
                .where(past_exam.c.id == old.scope_id, past_exam.c.transcript_task_id == old.id)
                .values(transcript_task_id=None, transcript_confirmed_at=None)
            )
            retired = _retire_edges(conn, topic_ids)

    log.info(
        json.dumps(
            {
                "event": "rescope_generation",
                "old_task_id": old.id,
                "new_task_id": new_task_id,
                "kind": old.kind,
                "scope_type": scope["scope_type"],
                "scope_id": scope["scope_id"],
                "superseded_items": superseded,
                "topics": sorted(topic_ids),
                "retired_edges": retired,
                "dropped_replacements": dropped,
            }
        )
    )
    subject = "past_exam_id" if old.kind == "transcription" else "material_id"
    return {
        "old_task_id": old.id,
        "new_task_id": new_task_id,
        "kind": old.kind,
        "scope_type": scope["scope_type"],
        "scope_id": scope["scope_id"],
        "item_count": scope["item_count"],
        "superseded_items": superseded,
        "retired_edges": len(retired),
        "dropped_replacements": dropped,
        "next": f"call study_start_generation with kind {old.kind} and {subject} "
        f"{scope['scope_id']} to fetch the new task",
    }


def _rescope_scope(conn: Connection, user_id: str, old, material_id, count) -> dict:
    """The new task's scope, or the refusal (D-264). A transcription keeps its paper and takes
    no new scope; a material's practice task may move to another material of his and take a new
    count. Refuses a task already retired, a kind with no item set, a topic task (its items fill
    queued holes), and a scope that already has another open task of the kind - two would leave
    study_start_generation unable to tell which to send.

    Returns {"scope_type", "scope_id", "item_count"}."""
    if old.status == "superseded":
        raise StudyError(
            code="task_closed",
            message="this task was already rescoped",
            fix=f"rescope the task that replaced it, {old.superseded_by}, if that one is wrong",
            field_errors=[{"field": "task_id", "problem": "task is superseded"}],
        )
    if old.status == "invalidated":
        raise StudyError(
            code="task_closed",
            message="this task was invalidated when its exam profile was derived again",
            fix="start a new one with study_start_generation - there is nothing to retire",
            field_errors=[{"field": "task_id", "problem": "task is invalidated"}],
        )
    if old.kind == "note":
        raise StudyError(
            code="not_supported",
            message="a note is not rescoped",
            fix="to replace a note, study_start_generation(kind='note', material_id, redo=true)",
            field_errors=[{"field": "task_id", "problem": "a note task"}],
        )
    if old.kind not in RESCOPABLE:
        raise StudyError(
            code="not_supported",
            message=f"a {old.kind} task holds no set of items to retire",
            fix="only a transcription or a practice task is rescoped",
            field_errors=[{"field": "task_id", "problem": f"a {old.kind} task"}],
        )
    if old.scope_type == "topic":
        raise StudyError(
            code="not_supported",
            message="a replacement task is not rescoped: its items fill queued holes",
            fix="reject each bad item with study_submit_feedback - each queues its own "
            "replacement (D-259)",
            field_errors=[{"field": "task_id", "problem": "a topic task"}],
        )
    if old.kind == "transcription" and (material_id is not None or count is not None):
        raise values.invalid(
            "material_id" if material_id is not None else "count",
            "a transcription keeps its paper - it takes no new scope",
            "send task_id alone",
        )

    scope = {"scope_type": old.scope_type, "scope_id": old.scope_id, "item_count": old.item_count}
    if material_id is not None:
        scope["scope_id"] = _find_material(conn, user_id, material_id).id
    if count is not None:
        scope["item_count"] = _whole_number("count", count)

    clash = conn.execute(
        select(generation_task.c.id).where(
            generation_task.c.user_id == user_id,
            generation_task.c.kind == old.kind,
            generation_task.c.scope_type == scope["scope_type"],
            generation_task.c.scope_id == scope["scope_id"],
            generation_task.c.status == "open",
            generation_task.c.id != old.id,
        )
    ).first()
    if clash is not None:
        raise StudyError(
            code="task_open",
            message=f"task {clash.id} is already open on this scope",
            fix=f"finish task {clash.id} first, or rescope it instead",
            field_errors=[{"field": "task_id", "problem": "an open task on the new scope"}],
        )
    return scope


def _supersede(conn: Connection, old_task, new_task_id: str) -> set[str]:
    """Retire `old_task` and every item on it, inside the caller's write unit. Returns the ids of
    the topics those items sat on - empty when the task holds no item.

    The task keeps its `closed_at` when it has one; an open task has none, and the schema needs
    one on every task that is not open. Every item goes, whatever its state: a rejected one that
    kept its state would keep its position (D-136), and the redo could not write it again (D-264).
    `new_task_id` is already a row - the caller inserts it first.
    """
    topic_ids = set(
        conn.execute(
            select(practice_item.c.topic_id.distinct()).where(
                practice_item.c.task_id == old_task.id,
            )
        )
        .scalars()
        .all()
    )
    conn.execute(
        update(practice_item)
        .where(
            practice_item.c.task_id == old_task.id,
        )
        .values(state="superseded")
    )
    conn.execute(
        update(generation_task)
        .where(
            generation_task.c.id == old_task.id,
        )
        .values(
            status="superseded",
            superseded_by=new_task_id,
            closed_at=old_task.closed_at if old_task.closed_at else now(),
        )
    )

    return topic_ids


def _retire_edges(conn: Connection, topic_ids: set[str]) -> list[str]:
    """After a transcription's items are superseded, retire each exam edge of `topic_ids` that
    no live question backs any more (D-261), inside the caller's write unit. Returns the ids of
    the edges retired - empty when none was.

    May retire: a topic -> exam edge (`assessment_id` set; a material edge comes from tagging, not
    questions), `inferred` and unconfirmed (declared or confirmed is his own word), on a topic not
    from a syllabus (the syllabus backs it), not yet retired (it keeps its first time). Retired
    when its exam is not one `exam_assessments` gives - the exams the topic's live past-exam
    questions come from; it reads live items only, so this runs after `_supersede`.
    """
    candidates = conn.execute(
        select(coverage.c.id, coverage.c.topic_id, coverage.c.assessment_id)
        .join_from(coverage, topic, topic.c.id == coverage.c.topic_id)
        .where(
            coverage.c.topic_id.in_(topic_ids),
            coverage.c.assessment_id.is_not(None),
            coverage.c.tier == "inferred",
            coverage.c.confirmed_at.is_(None),
            coverage.c.retired_at.is_(None),
            topic.c.proposed_by != "syllabus",
        )
        .order_by(coverage.c.id)
    ).all()

    backed: dict[str, set[str]] = {}  # topic -> the exams its live questions come from
    retired = []
    for edge in candidates:
        if edge.topic_id not in backed:
            backed[edge.topic_id] = set(exam_assessments(conn, edge.topic_id))
        if edge.assessment_id not in backed[edge.topic_id]:
            retired.append(edge.id)

    if retired:
        conn.execute(update(coverage).where(coverage.c.id.in_(retired)).values(retired_at=now()))
    return retired
