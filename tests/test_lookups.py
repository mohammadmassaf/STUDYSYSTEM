"""Lookups (D-38): a course by code (+ semester when the code repeats), an assessment by its slot
name - both case-insensitive, both blind to another user's rows."""

import pytest

from studysystem.errors import StudyError
from studysystem.services.assessments import insert_slot_and_assessment
from studysystem.services.courses import add_course
from studysystem.services.lookups import find_assessment, find_course
from studysystem.services.units import write_unit

SEM = "Semester 1 2026-2027"
RESIT = "Resit 2027-2028"


def lookup_error(engine, find, *args) -> StudyError:
    with engine.connect() as conn, pytest.raises(StudyError) as excinfo:
        find(conn, *args)
    return excinfo.value


# --- find_course ------------------------------------------------------------


def test_find_course_matches_the_code_in_any_case(service_engine, user_id):
    ids = add_course(service_engine, user_id, "I3302-E", "Server-Side Web Development", SEM)

    with service_engine.connect() as conn:
        assert find_course(conn, user_id, "i3302-e", None) == ids["course_id"]


def test_an_unknown_code_is_not_found(service_engine, user_id):
    add_course(service_engine, user_id, "I3302-E", "Server-Side Web Development", SEM)

    err = lookup_error(service_engine, find_course, user_id, "I3303", None)

    assert err.code == "not_found"
    assert err.field_errors[0]["field"] == "code"


def test_a_code_in_two_semesters_is_ambiguous_until_the_semester_is_given(service_engine, user_id):
    add_course(service_engine, user_id, "I3302-E", "Server-Side Web Development", SEM)
    resit = add_course(service_engine, user_id, "I3302-E", "Server-Side Web Development", RESIT)

    err = lookup_error(service_engine, find_course, user_id, "I3302-E", None)
    assert err.code == "ambiguous_course"
    assert SEM in err.fix and RESIT in err.fix

    with service_engine.connect() as conn:
        assert find_course(conn, user_id, "I3302-E", RESIT) == resit["course_id"]


def test_the_semester_matches_in_any_case(service_engine, user_id):
    add_course(service_engine, user_id, "I3302-E", "Server-Side Web Development", SEM)
    resit = add_course(service_engine, user_id, "I3302-E", "Server-Side Web Development", RESIT)

    with service_engine.connect() as conn:
        assert find_course(conn, user_id, "I3302-E", RESIT.upper()) == resit["course_id"]


def test_another_user_s_course_is_not_found(service_engine, user_id, other_user_id):
    add_course(service_engine, other_user_id, "I3302-E", "Someone else's", SEM)

    err = lookup_error(service_engine, find_course, user_id, "I3302-E", None)

    assert err.code == "not_found"


# --- find_assessment --------------------------------------------------------


def test_find_assessment_matches_the_slot_name_in_any_case(service_engine, user_id):
    ids = add_course(service_engine, user_id, "I3302-E", "Server-Side Web Development", SEM)

    with service_engine.connect() as conn:
        found = find_assessment(conn, user_id, ids["course_id"], "final EXAM")

    assert found == ids["assessment_id"]


def test_find_assessment_stays_inside_its_course(service_engine, user_id):
    add_course(service_engine, user_id, "I3301", "Databases", SEM)
    web = add_course(service_engine, user_id, "I3302-E", "Server-Side Web Development", SEM)

    with service_engine.connect() as conn:
        found = find_assessment(conn, user_id, web["course_id"], "Final exam")

    assert found == web["assessment_id"]  # not I3301's final, which has the same name


def test_a_near_miss_name_is_not_found_and_the_fix_lists_the_real_names(service_engine, user_id):
    ids = add_course(service_engine, user_id, "I3302-E", "Server-Side Web Development", SEM)
    with write_unit(service_engine) as conn:
        insert_slot_and_assessment(
            conn, user_id, ids["course_id"], "Midterm", "exam", 30, "declared"
        )

    err = lookup_error(service_engine, find_assessment, user_id, ids["course_id"], "Mid-term")

    assert err.code == "not_found"
    assert err.field_errors[0]["field"] == "assessment"
    assert "Midterm" in err.fix and "Final exam" in err.fix
