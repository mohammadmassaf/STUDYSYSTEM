"""System work on the Plan page (4.12; D-25, D-310, D-311): work on the system itself, not study -
past papers that move no marks, and directives waiting for his yes. It shows only on a measured
surplus, so it never competes with study time the plan needs.

Who reads it: `GET /state` (its `system_work` section). This module writes nothing. The page's
Confirm button writes through `POST /directives/confirm`, which calls
`feedback.confirm_directive` - the same service `study_confirm_directive` calls.
"""

from sqlalchemy import Connection, select

from studysystem.db.tables import course, generation_directive, plan, plan_item, topic

ALL_COURSES = "all courses"  # applies_to for a directive with no course: never a bare null


def system_work(conn: Connection, user_id: str, alert: dict) -> dict:
    """The rows of the Plan page's System work block, or two empty lists when there is no room.

    `alert` is `shortfall()`'s dict; only its "gap" is read. Room means a gap above 0 (D-310):
    a gap of 0, below 0, or None (no exam ahead) returns {"papers": [], "directives": []} and
    reads nothing else.

    Returns {
      "papers": one per `past-papers` item with marks_unlocked_estimate = 0 in the latest
                all-courses plan (the plan /state shows, D-163), shown or not - such an item
                ranks last, so it is almost never shown. A course dropped since is left out
                (D-217). Each {"course_id", "code", "setup_key", "reason"}, by code, then
                setup_key.
      "directives": this user's `proposed` directives, oldest first (created_at, then id).
                Each {"id", "kind", "text", "scope", "applies_to"} - scope and applies_to as
                active_directives gives them, except a directive for every course applies to
                ALL_COURSES, not None.
    }

    Examples, his data:
    - live, 2026-11-19: short by 8.83 h -> both lists empty, though the I3304 Partial item
      (estimate 0, shown 0) is in the latest plan
    - the same rows with a gap of +7.24 h -> papers: [I3304's Partial item]; I3301 Partial at
      120 and I3304 Final `more-papers` at 1.63 stay out
    - a proposed "Prefer short MCQ with four options." on I3302 -> scope "course", applies_to
      "I3302"; once confirmed it is `active` and leaves the list
    """
    gap = alert["gap"]
    if gap is None or gap <= 0:
        return {"papers": [], "directives": []}
    return {"papers": _papers(conn, user_id), "directives": _directives(conn, user_id)}


def _papers(conn: Connection, user_id: str) -> list[dict]:
    """The latest all-courses plan's zero-estimate past-papers items, shown or not."""
    latest = conn.execute(
        select(plan.c.id)
        .where(plan.c.user_id == user_id, plan.c.scope == "all-courses")
        .order_by(plan.c.id.desc())
        .limit(1)
    ).scalar()
    if latest is None:
        return []
    rows = conn.execute(
        select(plan_item.c.course_id, course.c.code, plan_item.c.setup_key, plan_item.c.reason)
        .join_from(plan_item, course, course.c.id == plan_item.c.course_id)
        .where(
            plan_item.c.plan_id == latest,
            plan_item.c.setup_kind == "past-papers",
            plan_item.c.marks_unlocked_estimate == 0,
            course.c.dropped_at.is_(None),
        )
        .order_by(course.c.code, plan_item.c.setup_key)
    ).mappings()
    return [dict(row) for row in rows]


def _directives(conn: Connection, user_id: str) -> list[dict]:
    """This user's proposed directives, oldest first, each with its scope as active_directives
    reads it."""
    # outer joins: a directive for every course has no course row, one for the course no topic
    rows = conn.execute(
        select(
            generation_directive.c.id,
            generation_directive.c.kind,
            generation_directive.c.text,
            topic.c.name,
            course.c.code,
        )
        .outerjoin(course, course.c.id == generation_directive.c.course_id)
        .outerjoin(topic, topic.c.id == generation_directive.c.topic_id)
        .where(
            generation_directive.c.user_id == user_id,
            generation_directive.c.status == "proposed",
        )
        .order_by(generation_directive.c.created_at, generation_directive.c.id)
    ).all()
    result = []
    for row in rows:
        if row.name is not None:
            scope, applies_to = "topic", row.name
        elif row.code is not None:
            scope, applies_to = "course", row.code
        else:
            scope, applies_to = "all", ALL_COURSES
        result.append(
            {
                "id": row.id,
                "kind": row.kind,
                "text": row.text,
                "scope": scope,
                "applies_to": applies_to,
            }
        )
    return result
