"""Finding a course by code and an assessment by name (D-38) - how every course-scoped tool turns
what Mohammad says ("I3302-E", "Midterm") into row ids.

Both take the caller's `conn`, never an engine: a setter looks up and writes inside one unit
(D-33). Both filter on `user_id`, so another student's course is `not_found`, never a match.
"""

from sqlalchemy import Connection, func, select

from studysystem.db.tables import assessment, assessment_slot, course
from studysystem.errors import StudyError


def find_course(conn: Connection, user_id: str, code: str, semester_name: str | None) -> str:
    """The id of this user's course with `code`, matched case-insensitively like the unique
    index. `semester_name` narrows it when the code exists in more than one semester."""
    query = select(course.c.id, course.c.semester_name).where(
        course.c.user_id == user_id, func.lower(course.c.code) == func.lower(code)
    )
    if semester_name is not None:
        query = query.where(func.lower(course.c.semester_name) == func.lower(semester_name))
    rows = conn.execute(query).all()
    if len(rows) == 0:
        where = f" in {semester_name}" if semester_name is not None else ""
        raise StudyError(
            code="not_found",
            message=f"no course {code}{where}",
            fix="check the code, or add the course with study_add_course",
            field_errors=[{"field": "code", "problem": "no course with this code"}],
        )
    if len(rows) >= 2:
        semesters = ", ".join(sorted(row.semester_name for row in rows))
        raise StudyError(
            code="ambiguous_course",
            message=f"{code} exists in {len(rows)} semesters",
            fix=f"call again with semester_name set to one of: {semesters}",
            field_errors=[
                {"field": "semester_name", "problem": "the code is in more than one semester"}
            ],
        )
    return rows[0].id


def find_assessment(conn: Connection, user_id: str, course_id: str, name: str) -> str:
    """The id of the assessment under this course's slot called `name` (case-insensitive).
    One per slot in 1.7 - nothing adds a resit sitting yet."""
    found = conn.execute(
        select(assessment.c.id)
        .join_from(assessment_slot, assessment)
        .where(
            assessment.c.user_id == user_id,
            assessment_slot.c.course_id == course_id,
            func.lower(assessment_slot.c.name) == func.lower(name),
        )
    ).scalar_one_or_none()

    if found is not None:
        return found

    names = (
        conn.execute(
            select(assessment_slot.c.name)
            .where(assessment_slot.c.course_id == course_id)
            .order_by(assessment_slot.c.name)
        )
        .scalars()
        .all()
    )
    raise StudyError(
        code="not_found",
        message=f"this course has no assessment called {name}",
        fix=f"use one of this course's assessments: {', '.join(names)}; "
        "or add a new one with study_add_assessment",
        field_errors=[{"field": "assessment", "problem": "no assessment with this name"}],
    )
