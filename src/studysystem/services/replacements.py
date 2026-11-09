"""The replacement queue (4.3; D-16 d and f, D-22, D-53, D-259): a rejected generated item, or an
item a practice task never landed, leaves a hole on its topic; a `pending` row remembers it until
a practice task scoped to that topic fills it.

Who writes what: `study_submit_feedback` queues a rejection, `study_submit_generation` queues a
practice task's partial close and fills rows from a topic task, and split / merge / decline move
or drop the rows on a topic they retire. Every call here runs inside its caller's write unit.
"""

from sqlalchemy import Connection, select, update

from studysystem.db.tables import generation_task, item_rejection, practice_item, replacement_queue
from studysystem.services.ids import now


def pending_rows(conn: Connection, user_id: str, topic_id: str) -> list:
    """The topic's `pending` rows, oldest first (ULIDs sort in the order they were made)."""
    return list(
        conn.execute(
            select(replacement_queue)
            .where(
                replacement_queue.c.user_id == user_id,
                replacement_queue.c.topic_id == topic_id,
                replacement_queue.c.status == "pending",
            )
            .order_by(replacement_queue.c.id)
        )
    )


def source_material(conn: Connection, row) -> str | None:
    """The material a row's hole came from, so its replacement is written from the same pages
    (D-259 item 3). A partial close: the closed task's material. A rejection: the rejected item's
    task's material - and when that task was itself a topic task, which has no material, the row
    it filled, followed back the same way. None when the chain ends without a material."""
    while True:
        if row.closed_task_id is not None:
            task_id = row.closed_task_id
        else:
            task_id = conn.execute(
                select(practice_item.c.task_id)
                .join(item_rejection, item_rejection.c.practice_item_id == practice_item.c.id)
                .where(item_rejection.c.id == row.rejection_id)
            ).scalar_one()
        task = conn.execute(
            select(generation_task.c.scope_type, generation_task.c.scope_id).where(
                generation_task.c.id == task_id
            )
        ).one()
        if task.scope_type == "material":
            return task.scope_id
        # a topic task: the oldest row it filled is where its own pages came from
        row = conn.execute(
            select(replacement_queue)
            .where(replacement_queue.c.filled_by_task_id == task_id)
            .order_by(replacement_queue.c.id)
            .limit(1)
        ).one_or_none()
        if row is None:
            return None


def fill(conn: Connection, user_id: str, task_id: str, topic_id: str) -> str | None:
    """One accepted item from a topic task fills the topic's oldest `pending` row. Returns the
    row's id, or None when no row is left - the item is kept all the same."""
    rows = pending_rows(conn, user_id, topic_id)
    if not rows:
        return None
    conn.execute(
        update(replacement_queue)
        .where(replacement_queue.c.id == rows[0].id)
        .values(status="filled", filled_by_task_id=task_id, resolved_at=now())
    )
    return rows[0].id


def retire(conn: Connection, topic_id: str, to: str | None = None) -> None:
    """A topic leaves the live set: its `pending` rows move to `to` (a merge's target, which
    takes every item, D-195) or, with no `to`, go `dropped` (a split or a decline - the rejected
    item stays on the old topic, so no single topic owns the hole). Filled rows stay as they are."""
    pending = (replacement_queue.c.topic_id == topic_id) & (replacement_queue.c.status == "pending")
    if to is not None:
        conn.execute(update(replacement_queue).where(pending).values(topic_id=to))
    else:
        conn.execute(
            update(replacement_queue).where(pending).values(status="dropped", resolved_at=now())
        )
