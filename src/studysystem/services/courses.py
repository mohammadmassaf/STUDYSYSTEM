"""Courses (D-17, D-37): add one with its default final, then set its declared inputs.

A new course knows only what identifies it. Everything else starts NULL with tier `unknown` -
never 0 - and moves to `declared` only when Mohammad states it.
"""

from sqlalchemy import Engine, func, insert, select, update  # noqa: F401

from studysystem.db.tables import course  # noqa: F401
from studysystem.errors import StudyError  # noqa: F401
from studysystem.services import values  # noqa: F401
from studysystem.services.assessments import insert_slot_and_assessment  # noqa: F401
from studysystem.services.ids import new_id, now  # noqa: F401
from studysystem.services.lookups import find_course  # noqa: F401
from studysystem.services.units import write_unit  # noqa: F401

# D-15: with no breakdown known, the final is the whole grade - an inference, not a fact.
DEFAULT_FINAL = "Final exam"

# What study_set_course_input accepts (D-37).
COURSE_FIELDS = ("credits", "instructor", "target_grade")

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
    # TODO(human):
    #   before the lock: code, name and semester are non-empty text
    #   in one unit:
    #     this user already has the code in this semester (any case) -> course_exists,
    #       the fix pointing at study_set_course_input
    #     the course row: new id, owner, code, name, semester; instructor and credits
    #       unknown (value NULL, tier unknown); target grade NULL; the rest by column default
    #     the default final through the helper: an exam, weight 100, tier inferred (D-15)
    code = values.text("code", code)
    name = values.text("name", name)
    semester_name = values.text("semester_name", semester_name)

    with write_unit(engine) as conn:
        existing = conn.execute(
            select(course.c.id).where(
                course.c.user_id == user_id,
                func.lower(course.c.code) == code.lower(),
                course.c.semester_name == semester_name,
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
    # TODO(human):
    #   before the lock:
    #     field not in COURSE_FIELDS -> unknown_field; the fix names the other home when
    #       OTHER_HOMES knows it, else lists COURSE_FIELDS
    #     no value -> invalid_value (D-37)
    #     check the value by field: credits above 0 | instructor text | target grade 0-100
    #   in one unit: find the course, update that one column - and its tier to declared,
    #     except target_grade, which has no tier column
    raise NotImplementedError
