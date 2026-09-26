"""Courses (D-17, D-37): add one with its default final, then set its declared inputs.

A new course knows only what identifies it. Everything else starts NULL with tier `unknown` -
never 0 - and moves to `declared` only when Mohammad states it.
"""

from sqlalchemy import Engine, func, insert, select, update

from studysystem.db.tables import course
from studysystem.errors import StudyError
from studysystem.services import values
from studysystem.services.assessments import insert_slot_and_assessment
from studysystem.services.ids import new_id, now
from studysystem.services.lookups import find_course
from studysystem.services.units import write_unit

# D-15: with no breakdown known, the final is the whole grade - an inference, not a fact.
DEFAULT_FINAL = "Final exam"

# What study_set_course_input accepts (D-37), each with its value check - the bounds are the
# table's CHECKs, so the service refuses first and the CHECK stays a backstop (D-36).
COURSE_FIELDS = {
    "credits": lambda v: values.number("credits", v, gt=0),
    "instructor": lambda v: values.text("instructor", v),
    "target_grade": lambda v: values.number("target_grade", v, ge=0, le=100),
}

# Fields that sound like course inputs but have another home - the unknown_field fix names it.
OTHER_HOMES = {
    "weight": "study_set_assessment_input",
    "date": "study_set_assessment_input",
    "exam_date": "study_set_assessment_input",
    "strategy": "study_respond_to_offer",
}


def add_course(engine: Engine, user_id: str, code: str, name: str, semester_name: str) -> dict:
    """The course, its default final slot and that final's sitting, as one unit (Write units).
    Returns {"course_id", "slot_id", "assessment_id"}."""
    code = values.text("code", code)
    name = values.text("name", name)
    semester_name = values.text("semester_name", semester_name)

    with write_unit(engine) as conn:
        existing = conn.execute(
            select(course.c.id).where(
                course.c.user_id == user_id,
                func.lower(course.c.code) == func.lower(code),
                func.lower(course.c.semester_name) == func.lower(semester_name),
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise StudyError(
                code="course_exists",
                message=f"{code} already exists in {semester_name}",
                fix="change it with study_set_course_input, or add it under another semester_name",
                field_errors=[{"field": "code", "problem": "already used this semester"}],
            )
        course_id = new_id()
        conn.execute(
            insert(course).values(
                id=course_id,
                user_id=user_id,
                code=code,
                name=name,
                semester_name=semester_name,
                instructor=None,
                instructor_tier="unknown",
                credits=None,
                credits_tier="unknown",
                target_grade=None,
                created_at=now(),
            )
        )
        ids = insert_slot_and_assessment(
            conn, user_id, course_id, DEFAULT_FINAL, "exam", 100, "inferred"
        )
    return {"course_id": course_id, **ids}


def set_course_input(
    engine: Engine,
    user_id: str,
    code: str,
    field: str,
    value: float | str | None,
    semester_name: str | None = None,
) -> dict:
    """Set one declared input on a course (D-37: setting declares, no way back to unknown).
    Returns {"course_id", "field", "value"}, plus the tier when the field has one."""
    if field not in COURSE_FIELDS:
        home = OTHER_HOMES.get(field)
        raise StudyError(
            code="unknown_field",
            message=f"{field} is not a course input",
            fix=f"set {field} with {home}"
            if home is not None
            else f"field must be one of: {', '.join(COURSE_FIELDS)}",
            field_errors=[{"field": "field", "problem": f"{field!r} is not a course input"}],
        )

    if value is None:
        raise values.invalid(
            field,
            "a set value cannot go back to unknown",
            f"send the {field} you know; leave it unset while it is unknown",
        )
    nb = COURSE_FIELDS[field](value)
    changes = {field: nb}
    result = {"field": field, "value": nb}
    if field != "target_grade":  # the one course input with no tier column
        changes[f"{field}_tier"] = "declared"
        result["tier"] = "declared"

    with write_unit(engine) as conn:
        course_id = find_course(conn, user_id, code, semester_name)
        conn.execute(update(course).values(changes).where(course.c.id == course_id))

    return {"course_id": course_id, **result}
