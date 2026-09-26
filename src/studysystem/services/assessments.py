"""Assessments (D-35): a slot and its sitting, added together, then their known-before-the-exam
inputs set one at a time.

The slot is the recurring component ("Final exam") and owns the past-paper pool; the
`assessment` row is this term's sitting - weight, date, time, room (ticket 21).
"""

from sqlalchemy import Connection, Engine, func, insert, select, update

from studysystem.db.tables import assessment, assessment_slot
from studysystem.errors import StudyError
from studysystem.services import values
from studysystem.services.ids import new_id, now
from studysystem.services.lookups import find_assessment, find_course
from studysystem.services.units import write_unit

KINDS = ("exam", "project", "lab")

# What study_set_assessment_input accepts, each with its value check - the bounds are the table's
# CHECKs (D-36). Status and mark are the post-exam flow's, not 1.7's. `date` is the odd one: its
# check returns a pair, (the date to store, whether it is approximate) - D-39.
ASSESSMENT_FIELDS = {
    "weight": lambda v: values.number("weight", v, ge=0, le=100),
    "date": lambda v: values.day_or_month("date", v),
    "time": lambda v: values.clock("time", v),
    "room": lambda v: values.text("room", v),
}

# Fields that sound like assessment inputs but belong to the course - the unknown_field fix names
# the tool that sets them.
OTHER_HOMES = {
    "credits": "study_set_course_input",
    "instructor": "study_set_course_input",
    "target_grade": "study_set_course_input",
}


def insert_slot_and_assessment(
    conn: Connection,
    user_id: str,
    course_id: str,
    name: str,
    kind: str,
    weight: float | None,
    weight_tier: str,
) -> dict:
    """Helper: one slot and its one sitting, on the caller's unit. Shared by `add_course` (the
    default final) and `add_assessment`, so the session-type rule lives in one place.
    Returns {"slot_id", "assessment_id"}."""

    slot_id = new_id()
    created_at = now()

    conn.execute(
        insert(assessment_slot).values(
            id=slot_id,
            owner_id=user_id,
            course_id=course_id,
            name=name,
            kind=kind,
            created_at=created_at,
        )
    )
    assessment_id = new_id()

    conn.execute(
        insert(assessment).values(
            id=assessment_id,
            user_id=user_id,
            slot_id=slot_id,
            weight=weight,
            weight_tier=weight_tier,
            session_type="first" if kind == "exam" else None,
            created_at=created_at,
        )
    )

    return {"slot_id": slot_id, "assessment_id": assessment_id}


def add_assessment(
    engine: Engine,
    user_id: str,
    code: str,
    name: str,
    kind: str,
    weight: float | str | None = None,
    semester_name: str | None = None,
) -> dict:
    """A new slot + sitting on an existing course. A weight given is declared; none is unknown.
    Returns {"course_id", "slot_id", "assessment_id"}."""
    name = values.text("name", name)
    if weight is not None:
        weight = values.number("weight", weight, ge=0, le=100)
    if kind not in KINDS:
        raise values.invalid("kind", f"{kind!r} is not a kind", f"send one of: {', '.join(KINDS)}")
    with write_unit(engine) as conn:
        course_id = find_course(conn, user_id, code, semester_name)
        slot_exist = conn.execute(
            select(assessment_slot.c.id).where(
                assessment_slot.c.course_id == course_id,
                func.lower(assessment_slot.c.name) == func.lower(name),
            )
        ).scalar_one_or_none()
        if slot_exist is not None:
            raise StudyError(
                code="slot_exists",
                message=f"this course already has an assessment called {name}",
                fix="change its weight, date, time or room with study_set_assessment_input",
                field_errors=[{"field": "name", "problem": "already used in this course"}],
            )
        ids = insert_slot_and_assessment(
            conn,
            user_id,
            course_id,
            name,
            kind,
            weight,
            "declared" if weight is not None else "unknown",
        )
    return {"course_id": course_id, **ids}


def set_assessment_input(
    engine: Engine,
    user_id: str,
    code: str,
    assessment_name: str,
    field: str,
    value: float | str | None,
    semester_name: str | None = None,
) -> dict:
    """Set one input on a sitting (D-37: setting declares, no way back to unknown).
    Returns {"assessment_id", "field", "value"} plus the tier or approx flag it changed."""
    if field not in ASSESSMENT_FIELDS:
        home = OTHER_HOMES.get(field)
        raise StudyError(
            code="unknown_field",
            message=f"{field} is not an assessment input",
            fix=f"set {field} with {home}"
            if home is not None
            else f"field must be one of: {', '.join(ASSESSMENT_FIELDS)}",
            field_errors=[{"field": "field", "problem": f"{field!r} is not an assessment input"}],
        )
    if value is None:
        raise values.invalid(
            field,
            "a set value cannot go back to unknown",
            f"send the {field} you know; leave it unset while it is unknown",
        )
    nb = ASSESSMENT_FIELDS[field](value)
    if field == "date":  # D-39: the checker hands back (date, approximate?) - two columns
        nb, approx = nb
        changes = {"date": nb, "date_approx": int(approx)}  # 0/1 column, not a boolean
        result = {"field": field, "value": nb, "date_approx": int(approx)}
    else:
        changes = {field: nb}
        result = {"field": field, "value": nb}
    if field == "weight":
        changes["weight_tier"] = "declared"
        result["tier"] = "declared"

    with write_unit(engine) as conn:
        course_id = find_course(conn, user_id, code, semester_name)
        assessment_id = find_assessment(conn, user_id, course_id, assessment_name)
        conn.execute(update(assessment).values(changes).where(assessment.c.id == assessment_id))

    return {"assessment_id": assessment_id, **result}
