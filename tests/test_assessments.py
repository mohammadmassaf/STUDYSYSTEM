"""Assessments (D-35, D-37, D-39): a sitting's inputs are set one field at a time, each set
declares, and a month-only date is the 1st marked approximate."""

import pytest
from sqlalchemy import func, select

from studysystem.db.tables import assessment, assessment_slot
from studysystem.errors import StudyError
from studysystem.services.assessments import (
    add_assessment,
    insert_slot_and_assessment,
    set_assessment_input,
)
from studysystem.services.courses import add_course
from studysystem.services.units import write_unit

SEM = "Semester 1 2026-2027"


@pytest.fixture
def web(service_engine, user_id):
    """I3302 with its default final and a 30% midterm."""
    ids = add_course(service_engine, user_id, "I3302-E", "Server-Side Web Development", SEM)
    with write_unit(service_engine) as conn:
        mid = insert_slot_and_assessment(
            conn, user_id, ids["course_id"], "Midterm", "exam", 30, "declared"
        )
    return {**ids, "midterm_id": mid["assessment_id"]}


def sitting(engine, assessment_id):
    with engine.connect() as conn:
        return (
            conn.execute(select(assessment).where(assessment.c.id == assessment_id)).one()._mapping
        )


def set_final(engine, user_id, field, value):
    return set_assessment_input(engine, user_id, "I3302-E", "Final exam", field, value)


def as_it_was(row) -> bool:
    """The final exactly as `add_course` left it."""
    return (
        (row["weight"], row["weight_tier"]) == (100, "inferred")
        and (row["date"], row["date_approx"]) == (None, 0)
        and row["time"] is None
        and row["room"] is None
    )


# --- each field -------------------------------------------------------------


def test_setting_the_weight_declares_it(service_engine, user_id, web):
    result = set_final(service_engine, user_id, "weight", 70)

    assert result == {
        "assessment_id": web["assessment_id"],
        "field": "weight",
        "value": 70,
        "tier": "declared",
    }
    a = sitting(service_engine, web["assessment_id"])
    assert (a["weight"], a["weight_tier"]) == (70, "declared")
    assert sitting(service_engine, web["midterm_id"])["weight"] == 30  # only the one named


def test_an_exact_date_is_stored_not_approximate(service_engine, user_id, web):
    result = set_final(service_engine, user_id, "date", "2027-01-18")

    assert result["value"] == "2027-01-18" and result["date_approx"] == 0
    assert "tier" not in result
    a = sitting(service_engine, web["assessment_id"])
    assert (a["date"], a["date_approx"]) == ("2027-01-18", 0)


def test_a_month_only_date_is_the_first_marked_approximate(service_engine, user_id, web):
    """D-39: "January 2027" is stored as the 1st - the safe wrong, nearer rather than further."""
    result = set_final(service_engine, user_id, "date", "2027-01")

    assert result["value"] == "2027-01-01" and result["date_approx"] == 1
    a = sitting(service_engine, web["assessment_id"])
    assert (a["date"], a["date_approx"]) == ("2027-01-01", 1)


def test_a_later_exact_date_clears_the_approximate_flag(service_engine, user_id, web):
    set_final(service_engine, user_id, "date", "2027-01")
    set_final(service_engine, user_id, "date", "2027-01-18")

    a = sitting(service_engine, web["assessment_id"])
    assert (a["date"], a["date_approx"]) == ("2027-01-18", 0)


def test_setting_the_time_and_room(service_engine, user_id, web):
    assert set_final(service_engine, user_id, "time", "09:00") == {
        "assessment_id": web["assessment_id"],
        "field": "time",
        "value": "09:00",
    }
    set_final(service_engine, user_id, "room", "  Hall B  ")

    a = sitting(service_engine, web["assessment_id"])
    assert (a["time"], a["room"]) == ("09:00", "Hall B")
    assert (a["weight"], a["weight_tier"]) == (100, "inferred")  # one field, its own columns


def test_the_assessment_name_matches_in_any_case(service_engine, user_id, web):
    set_assessment_input(service_engine, user_id, "i3302-e", "MIDTERM", "weight", 25)

    assert sitting(service_engine, web["midterm_id"])["weight"] == 25


# --- refused ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("weight", 150),
        ("weight", -5),
        ("weight", None),  # D-37: no way back to unknown
        ("date", "2027-02-30"),
        ("date", "2027-13"),
        ("date", "18/01/2027"),
        ("time", "9am"),
        ("time", "24:00"),
        ("room", "   "),
    ],
)
def test_a_bad_value_is_invalid_and_changes_nothing(service_engine, user_id, web, field, value):
    with pytest.raises(StudyError) as excinfo:
        set_final(service_engine, user_id, field, value)

    assert excinfo.value.code == "invalid_value"
    assert excinfo.value.field_errors
    assert as_it_was(sitting(service_engine, web["assessment_id"]))


@pytest.mark.parametrize("field", ["credits", "instructor", "target_grade"])
def test_a_course_field_is_unknown_field_naming_the_course_setter(
    service_engine, user_id, web, field
):
    with pytest.raises(StudyError) as excinfo:
        set_final(service_engine, user_id, field, 3)

    assert excinfo.value.code == "unknown_field"
    assert "study_set_course_input" in excinfo.value.fix


def test_an_unknown_field_lists_the_assessment_fields(service_engine, user_id, web):
    with pytest.raises(StudyError) as excinfo:
        set_final(service_engine, user_id, "status", "sat")

    assert excinfo.value.code == "unknown_field"
    assert all(f in excinfo.value.fix for f in ("weight", "date", "time", "room"))


def test_a_near_miss_name_is_not_found_and_lists_the_real_names(service_engine, user_id, web):
    with pytest.raises(StudyError) as excinfo:
        set_assessment_input(service_engine, user_id, "I3302-E", "Mid-term", "weight", 30)

    assert excinfo.value.code == "not_found"
    assert "Midterm" in excinfo.value.fix and "Final exam" in excinfo.value.fix


def test_an_unknown_course_is_not_found(service_engine, user_id, web):
    with pytest.raises(StudyError) as excinfo:
        set_assessment_input(service_engine, user_id, "I3303", "Final exam", "weight", 60)

    assert excinfo.value.code == "not_found"
    assert excinfo.value.field_errors[0]["field"] == "code"


# --- add_assessment (D-35) --------------------------------------------------


@pytest.fixture
def web_bare(service_engine, user_id):
    """I3302 as add_course leaves it: the default final only."""
    return add_course(service_engine, user_id, "I3302-E", "Server-Side Web Development", SEM)


def slots(engine, course_id) -> dict:
    """{slot name: (kind, weight, weight_tier, session_type)} for the course."""
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                assessment_slot.c.name,
                assessment_slot.c.kind,
                assessment.c.weight,
                assessment.c.weight_tier,
                assessment.c.session_type,
            )
            .join_from(assessment_slot, assessment)
            .where(assessment_slot.c.course_id == course_id)
        ).all()
    return {r.name: (r.kind, r.weight, r.weight_tier, r.session_type) for r in rows}


def slot_count(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(assessment_slot)).scalar_one()


def test_a_midterm_with_a_weight_is_declared_and_the_final_is_untouched(
    service_engine, user_id, web_bare
):
    ids = add_assessment(service_engine, user_id, "I3302-E", "Midterm", "exam", 30)

    assert set(ids) == {"course_id", "slot_id", "assessment_id"}
    assert ids["course_id"] == web_bare["course_id"]
    assert slots(service_engine, web_bare["course_id"]) == {
        "Final exam": ("exam", 100, "inferred", "first"),  # no rebalancing (D-35)
        "Midterm": ("exam", 30, "declared", "first"),
    }


def test_a_weight_as_a_numeric_string_is_stored_as_a_number(service_engine, user_id, web_bare):
    add_assessment(service_engine, user_id, "i3302-e", "Midterm", "exam", "30")

    assert slots(service_engine, web_bare["course_id"])["Midterm"][1:3] == (30.0, "declared")


def test_no_weight_is_unknown_never_zero(service_engine, user_id, web_bare):
    add_assessment(service_engine, user_id, "I3302-E", "Midterm", "exam")

    assert slots(service_engine, web_bare["course_id"])["Midterm"] == (
        "exam",
        None,
        "unknown",
        "first",
    )


def test_a_project_has_no_session_type(service_engine, user_id, web_bare):
    add_assessment(service_engine, user_id, "I3302-E", "Project", "project", 20)

    assert slots(service_engine, web_bare["course_id"])["Project"] == (
        "project",
        20,
        "declared",
        None,
    )


def test_a_name_the_course_already_has_in_any_case_is_slot_exists(
    service_engine, user_id, web_bare
):
    with pytest.raises(StudyError) as excinfo:
        add_assessment(service_engine, user_id, "I3302-E", "final EXAM", "exam", 60)

    assert excinfo.value.code == "slot_exists"
    assert "study_set_assessment_input" in excinfo.value.fix
    assert slot_count(service_engine) == 1


@pytest.mark.parametrize(
    ("name", "kind", "weight", "field"),
    [
        ("Midterm", "exam", 150, "weight"),
        ("Midterm", "exam", -1, "weight"),
        ("Midterm", "exam", "thirty", "weight"),
        ("Quiz 1", "quiz", 10, "kind"),
        ("   ", "exam", 30, "name"),
    ],
)
def test_a_bad_input_is_invalid_and_writes_nothing(
    service_engine, user_id, web_bare, name, kind, weight, field
):
    with pytest.raises(StudyError) as excinfo:
        add_assessment(service_engine, user_id, "I3302-E", name, kind, weight)

    assert excinfo.value.code == "invalid_value"
    assert excinfo.value.field_errors[0]["field"] == field
    assert slot_count(service_engine) == 1


def test_adding_to_an_unknown_course_is_not_found(service_engine, user_id, web_bare):
    with pytest.raises(StudyError) as excinfo:
        add_assessment(service_engine, user_id, "I3303", "Midterm", "exam", 30)

    assert excinfo.value.code == "not_found"
    assert slot_count(service_engine) == 1
