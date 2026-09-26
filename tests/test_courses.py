"""Courses (D-17, D-37): `add_course` writes three rows as one unit, and everything not stated
starts NULL with tier `unknown` - never 0."""

import pytest
from sqlalchemy import func, select

from studysystem.db.tables import assessment, assessment_slot, course
from studysystem.errors import StudyError
from studysystem.services.courses import add_course, set_course_input

SEM = "Semester 1 2026-2027"
RESIT = "Resit 2027-2028"


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
    add_course(service_engine, user_id, "I3302-E", "Server-Side Web Development", RESIT)

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


def test_the_same_semester_in_another_case_is_course_exists(service_engine, user_id):
    """D-40: a retyped "semester 1" finds the course, never makes a second one."""
    add_course(service_engine, user_id, "I3302-E", "Server-Side Web Development", SEM)

    with pytest.raises(StudyError) as excinfo:
        add_course(service_engine, user_id, "I3302-E", "Server-Side Web Development", SEM.lower())

    assert excinfo.value.code == "course_exists"
    assert counts(service_engine) == (1, 1, 1)


# --- set_course_input (D-37) -----------------------------------------------


@pytest.fixture
def web(service_engine, user_id):
    return add_course(service_engine, user_id, "I3302-E", "Server-Side Web Development", SEM)


def course_row(engine, course_id):
    with engine.connect() as conn:
        return conn.execute(select(course).where(course.c.id == course_id)).one()._mapping


def untouched(row) -> bool:
    """Every input still as `add_course` left it."""
    return (
        (row["credits"], row["credits_tier"]) == (None, "unknown")
        and (row["instructor"], row["instructor_tier"]) == (None, "unknown")
        and row["target_grade"] is None
    )


def test_setting_credits_stores_them_declared(service_engine, user_id, web):
    result = set_course_input(service_engine, user_id, "I3302-E", "credits", 3)

    assert result == {
        "course_id": web["course_id"],
        "field": "credits",
        "value": 3,
        "tier": "declared",
    }
    c = course_row(service_engine, web["course_id"])
    assert (c["credits"], c["credits_tier"]) == (3, "declared")
    assert (c["instructor"], c["instructor_tier"]) == (None, "unknown")  # one field, one column


def test_credits_as_a_numeric_string_are_stored_as_a_number(service_engine, user_id, web):
    set_course_input(service_engine, user_id, "i3302-e", "credits", "3")  # Cowork may send "3"

    assert course_row(service_engine, web["course_id"])["credits"] == 3.0


def test_setting_the_instructor_declares_it_stripped(service_engine, user_id, web):
    set_course_input(service_engine, user_id, "I3302-E", "instructor", "  Dr. Name  ")

    c = course_row(service_engine, web["course_id"])
    assert (c["instructor"], c["instructor_tier"]) == ("Dr. Name", "declared")


def test_target_grade_is_stored_and_has_no_tier(service_engine, user_id, web):
    result = set_course_input(service_engine, user_id, "I3302-E", "target_grade", 85)

    assert result == {"course_id": web["course_id"], "field": "target_grade", "value": 85}
    assert course_row(service_engine, web["course_id"])["target_grade"] == 85


def test_a_second_set_overwrites_the_first(service_engine, user_id, web):
    """D-37's accepted cost: a wrong declaration is fixed by another value, never withdrawn."""
    set_course_input(service_engine, user_id, "I3302-E", "credits", 3)
    set_course_input(service_engine, user_id, "I3302-E", "credits", 4)

    c = course_row(service_engine, web["course_id"])
    assert (c["credits"], c["credits_tier"]) == (4, "declared")


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("credits", 0),
        ("credits", -2),
        ("credits", "three"),
        ("credits", None),  # D-37: no way back to unknown
        ("instructor", "   "),
        ("instructor", None),
        ("target_grade", 101),
        ("target_grade", -1),
        ("target_grade", None),
    ],
)
def test_a_bad_value_is_invalid_and_changes_nothing(service_engine, user_id, web, field, value):
    with pytest.raises(StudyError) as excinfo:
        set_course_input(service_engine, user_id, "I3302-E", field, value)

    assert excinfo.value.code == "invalid_value"
    assert excinfo.value.field_errors
    assert untouched(course_row(service_engine, web["course_id"]))


@pytest.mark.parametrize(
    ("field", "home"),
    [
        ("weight", "study_set_assessment_input"),
        ("date", "study_set_assessment_input"),
        ("strategy", "study_respond_to_offer"),
    ],
)
def test_a_field_with_another_home_is_unknown_field_naming_that_tool(
    service_engine, user_id, web, field, home
):
    with pytest.raises(StudyError) as excinfo:
        set_course_input(service_engine, user_id, "I3302-E", field, 30)

    assert excinfo.value.code == "unknown_field"
    assert excinfo.value.field_errors[0]["field"] == "field"
    assert home in excinfo.value.fix
    assert untouched(course_row(service_engine, web["course_id"]))


def test_an_unknown_field_lists_the_course_fields(service_engine, user_id, web):
    with pytest.raises(StudyError) as excinfo:
        set_course_input(service_engine, user_id, "I3302-E", "colour", "blue")

    assert excinfo.value.code == "unknown_field"
    assert all(f in excinfo.value.fix for f in ("credits", "instructor", "target_grade"))


def test_an_unknown_code_is_not_found(service_engine, user_id, web):
    with pytest.raises(StudyError) as excinfo:
        set_course_input(service_engine, user_id, "I3303", "credits", 3)

    assert excinfo.value.code == "not_found"


def test_a_code_in_two_semesters_needs_the_semester(service_engine, user_id, web):
    resit = add_course(service_engine, user_id, "I3302-E", "Server-Side Web Development", RESIT)

    with pytest.raises(StudyError) as excinfo:
        set_course_input(service_engine, user_id, "I3302-E", "credits", 3)
    assert excinfo.value.code == "ambiguous_course"

    set_course_input(service_engine, user_id, "I3302-E", "credits", 3, semester_name=RESIT)
    assert course_row(service_engine, resit["course_id"])["credits"] == 3
    assert untouched(course_row(service_engine, web["course_id"]))


def test_another_user_s_course_cannot_be_set(service_engine, user_id, other_user_id):
    theirs = add_course(service_engine, other_user_id, "I3302-E", "Someone else's", SEM)

    with pytest.raises(StudyError) as excinfo:
        set_course_input(service_engine, user_id, "I3302-E", "credits", 3)

    assert excinfo.value.code == "not_found"
    assert untouched(course_row(service_engine, theirs["course_id"]))
