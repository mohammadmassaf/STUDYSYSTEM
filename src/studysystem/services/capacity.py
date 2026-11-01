"""Weekly study capacity, dated and insert-only (D-17). The capacity in force on day D is the row
with the latest `from_date <= D`, ties to the later id - read by the scheduler's shortfall
detector (D-15), not by anything in 1.7. A break is two rows: its own hours from its start,
then the term's hours from the return. Hours are always > 0 - a week with no study is not stored.
"""

from sqlalchemy import Connection, Engine, insert, select, update

from studysystem.db.tables import capacity, capacity_suggestion, class_meeting, course
from studysystem.errors import StudyError
from studysystem.services import values
from studysystem.services.ids import new_id, now
from studysystem.services.units import write_unit


def set_capacity(engine: Engine, user_id: str, hours_per_week: float | str, from_date: str) -> dict:
    """One new capacity row. Never updates an old one.
    Returns {"capacity_id", "hours_per_week", "from_date"}."""
    hours_per_week = values.number("hours_per_week", hours_per_week, gt=0, le=112)

    date = values.day("from_date", from_date)
    with write_unit(engine) as conn:
        capacity_id = _insert_capacity(conn, user_id, hours_per_week, date, now())

    return {"capacity_id": capacity_id, "hours_per_week": hours_per_week, "from_date": date}


def _insert_capacity(
    conn: Connection, user_id: str, hours_per_week: float, from_date: str, stamp: str
) -> str:
    """One capacity row, checked values in, inside the caller's unit. Returns its id."""
    capacity_id = new_id()
    conn.execute(
        insert(capacity).values(
            id=capacity_id,
            user_id=user_id,
            hours_per_week=hours_per_week,
            from_date=from_date,
            created_at=stamp,
        )
    )
    return capacity_id


STUDY_PER_CLASS_HOUR = 0.5  # D-216: 20 lecture hours a week suggest 10 study hours


def suggested_hours(meetings: list[dict]) -> float | None:
    """The study hours a timetable suggests: the minutes of its `lecture` meetings, in hours,
    times STUDY_PER_CLASS_HOUR. Labs and tutorials do not count; a parity meeting counts in full.
    None when no lecture is left - unknown, not 0 (the column refuses 0). `meetings` are dicts
    with start_time, end_time ("HH:MM") and kind; no database in here."""
    minutes = sum(
        _minutes(m["end_time"]) - _minutes(m["start_time"])
        for m in meetings
        if m["kind"] == "lecture"
    )
    if minutes <= 0:
        return None
    return round(minutes / 60 * STUDY_PER_CLASS_HOUR, 2)


def recount_suggestion(
    conn: Connection, user_id: str, task_id: str
) -> tuple[float | None, str | None]:
    """His one pending suggestion, counted again from his whole week - every meeting he holds,
    from any task, lectures of planned courses he still takes (D-216, D-217). Called by each
    timetable submission (`task_id` = that task) and by dropping a course (`task_id` = the
    pending row's own). A changed number replaces the pending row, the old one `dismissed`; the
    same number keeps it. Returns the hours and the pending row's id, or (None, None) when
    nothing counts (no row - unknown, not 0)."""
    kept = conn.execute(
        select(class_meeting.c.start_time, class_meeting.c.end_time, class_meeting.c.kind)
        .join_from(class_meeting, course, course.c.id == class_meeting.c.course_id)
        .where(
            class_meeting.c.user_id == user_id,
            course.c.planned == 1,
            course.c.dropped_at.is_(None),
        )
    ).mappings()
    hours = suggested_hours([dict(m) for m in kept])

    pending = conn.execute(
        select(capacity_suggestion).where(
            capacity_suggestion.c.user_id == user_id,
            capacity_suggestion.c.status == "pending",
        )
    ).one_or_none()  # at most one: every recount retires the one before
    if pending is not None and pending.hours_per_week == hours:
        return hours, pending.id
    stamp = now()
    if pending is not None:
        conn.execute(
            update(capacity_suggestion)
            .where(capacity_suggestion.c.id == pending.id)
            .values(status="dismissed", resolved_at=stamp)
        )
    if hours is None:
        return None, None
    suggestion_id = new_id()
    conn.execute(
        insert(capacity_suggestion).values(
            id=suggestion_id,
            user_id=user_id,
            hours_per_week=hours,
            source_task_id=task_id,
            status="pending",
            created_at=stamp,
        )
    )
    return hours, suggestion_id


def _minutes(hhmm: str) -> int:
    """'14:30' -> 870, minutes since midnight."""
    hours, mins = hhmm.split(":")
    return int(hours) * 60 + int(mins)


def accept_capacity_suggestion(
    engine: Engine, user_id: str, suggestion_id: str, from_date: str
) -> dict:
    """Accept a pending suggestion: one new capacity row and the suggestion marked accepted, as
    one write unit (D-216). The declared rows stay; the new one wins from `from_date` on.
    Returns the same dict as `set_capacity`."""
    date = values.day("from_date", from_date)  # before the unit: a bad date writes nothing

    with write_unit(engine) as conn:
        found = conn.execute(
            select(capacity_suggestion).where(
                capacity_suggestion.c.id == suggestion_id,
                capacity_suggestion.c.user_id == user_id,
            )
        ).one_or_none()
        if found is None:
            raise StudyError(
                code="not_found",
                message=f"no capacity suggestion {suggestion_id!r}",
                fix="read the pending suggestion's id from the dashboard state",
                field_errors=[{"field": "suggestion_id", "problem": "no such suggestion"}],
            )
        if found.status != "pending":
            raise StudyError(
                code="already_resolved",
                message=f"this suggestion is already {found.status}",
                fix="nothing to do - read the timetable again for a new suggestion",
                field_errors=[{"field": "suggestion_id", "problem": f"already {found.status}"}],
            )
        # the capacity CHECK's own bound, as a structured error instead of a failed insert
        hours = values.number("hours_per_week", found.hours_per_week, gt=0, le=112)
        stamp = now()
        capacity_id = _insert_capacity(conn, user_id, hours, date, stamp)
        conn.execute(
            update(capacity_suggestion)
            .where(capacity_suggestion.c.id == found.id)
            .values(status="accepted", resolved_at=stamp)
        )

    return {"capacity_id": capacity_id, "hours_per_week": hours, "from_date": date}
