"""The dogfood gate (1.17; D-21, D-22, D-113, D-116, D-117): a milestone closes only when the
database proves use. M1's proof is an `hours_entry` logged against a `plan_item` the server
ranked and showed, in the plan in force when the work started.

Read only: one query, no write unit. The tool `study_get_gate` and, later, the dashboard panel
call this same function.
"""

from sqlalchemy import Engine, and_, or_, select

from studysystem.db.tables import hours_entry, plan, plan_item


def dogfood_gate(engine: Engine, user_id: str) -> dict:
    """Every hour of `user_id`'s that proves use, and whether there is any.

    An hour counts when it is not voided and the plan in force at its `occurred_at` - the newest
    plan created at or before it, ties broken by id - holds a `plan_item` that is shown and is
    about the same thing: the same `topic_id`, or the same `setup_key` for a setup hour. A
    course-level hour (both NULL) matches nothing; an hour before any plan has no plan in force.

    Returns {"used": bool, "rows": [...]}, rows oldest first, each:
    {"hours_entry_id", "course_id", "topic_id", "setup_key", "minutes", "occurred_at",
     "plan_id", "plan_item_id", "lane", "rank", "reason"}. `used` is whether `rows` is non-empty.
    One row per matching item, not per hour: a topic shown in both the review and the study lane
    lists its hour twice (D-117), so minutes are never summed over these rows.
    """
    # The plan in force for the outer hour: this user's newest plan made at or before it. Two
    # plans in one second fall back to the id - a ULID, so the later-made plan sorts last.
    in_force = (
        select(plan.c.id)
        .where(plan.c.user_id == user_id, plan.c.created_at <= hours_entry.c.occurred_at)
        .order_by(plan.c.created_at.desc(), plan.c.id.desc())
        .limit(1)
        .correlate(hours_entry)
        .scalar_subquery()
    )
    proves = and_(
        plan_item.c.plan_id == in_force,
        or_(
            plan_item.c.topic_id == hours_entry.c.topic_id,
            plan_item.c.setup_key == hours_entry.c.setup_key,
        ),
        plan_item.c.shown == 1,
    )
    query = (
        select(
            hours_entry.c.id.label("hours_entry_id"),
            hours_entry.c.course_id,
            hours_entry.c.topic_id,
            hours_entry.c.setup_key,
            hours_entry.c.minutes,
            hours_entry.c.occurred_at,
            plan_item.c.plan_id,
            plan_item.c.id.label("plan_item_id"),
            plan_item.c.lane,
            plan_item.c.rank,
            plan_item.c.reason,
        )
        .join_from(hours_entry, plan_item, proves)
        .where(hours_entry.c.user_id == user_id, hours_entry.c.voided_at.is_(None))
        .order_by(hours_entry.c.occurred_at, hours_entry.c.id)
    )
    with engine.connect() as conn:
        rows = [dict(row._mapping) for row in conn.execute(query)]
    return {"used": bool(rows), "rows": rows}
