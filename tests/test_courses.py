"""Courses (D-17, D-37): `add_course` writes three rows as one unit, and everything not stated
starts NULL with tier `unknown` - never 0."""

import pytest
from sqlalchemy import func, select

from studysystem.db.tables import assessment, assessment_slot, course
from studysystem.errors import StudyError
from studysystem.services.courses import add_course
from studysystem.services.users import ensure_user

SEM = "Semester 1 2026-2027"


@pytest.fixture
def user_id(service_engine):
    return ensure_user(service_engine)


def counts(engine) -> tuple[int, int, int]:
    with engine.connect() as conn:
        return tuple(
            conn.execute(select(func.count()).select_from(t)).scalar_one()
            for t in (course, assessment_slot, assessment)
        )


def test_add_course_writes_the_course_its_final_and_that_final_s_sitting(service_engine, user_id):
    ids = add_course(service_engine, user_id, "I3302-E", "Server-Side Web Development", SEM)

    with service_engine.connect() as conn:
        c = conn.execute(select(course)).one()._mapping
        s = conn.execute(select(assessment_slot)).one()._mapping
        a = conn.execute(select(assessment)).one()._mapping

    assert ids == {"course_id": c["id"], "slot_id": s["id"], "assessment_id": a["id"]}
    assert c["id"] < s["id"] < a["id"]  # made in order, sort in order (D-32)

    assert (c["user_id"], c["code"], c["name"], c["semester_name"]) == (
        user_id,
        "I3302-E",
        "Server-Side Web Development",
        SEM,
    )
    assert (c["credits"], c["credits_tier"]) == (None, "unknown")
    assert (c["instructor"], c["instructor_tier"]) == (None, "unknown")
    assert c["target_grade"] is None
    assert (c["strategy"], c["conceded"]) == ("default", 0)

    assert (s["owner_id"], s["course_id"], s["name"], s["kind"]) == (
        user_id,
        c["id"],
        "Final exam",
        "exam",
    )

    assert (a["user_id"], a["slot_id"]) == (user_id, s["id"])
    assert (a["weight"], a["weight_tier"]) == (100, "inferred")  # D-15
    assert a["session_type"] == "first"
    assert (a["date"], a["date_approx"], a["status"]) == (None, 0, "upcoming")


def test_the_same_code_in_the_same_semester_in_any_case_is_course_exists(service_engine, user_id):
    add_course(service_engine, user_id, "I3302-E", "Server-Side Web Development", SEM)

    with pytest.raises(StudyError) as excinfo:
        add_course(service_engine, user_id, "i3302-e", "Server-Side Web Development", SEM)

    assert excinfo.value.code == "course_exists"
    assert "study_set_course_input" in excinfo.value.fix
    assert counts(service_engine) == (1, 1, 1)  # nothing half-written by the second call


def test_the_same_code_in_another_semester_is_a_new_course(service_engine, user_id):
    add_course(service_engine, user_id, "I3302-E", "Server-Side Web Development", SEM)
    add_course(service_engine, user_id, "I3302-E", "Server-Side Web Development", "Resit 2027-2028")

    assert counts(service_engine) == (2, 2, 2)


@pytest.mark.parametrize(
    ("code", "name", "field"),
    [("", "Server-Side Web Development", "code"), ("I3302-E", "   ", "name")],
)
def test_an_empty_input_is_invalid_and_writes_nothing(service_engine, user_id, code, name, field):
    with pytest.raises(StudyError) as excinfo:
        add_course(service_engine, user_id, code, name, SEM)

    assert excinfo.value.code == "invalid_value"
    assert excinfo.value.field_errors[0]["field"] == field
    assert counts(service_engine) == (0, 0, 0)
