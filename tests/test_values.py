"""Value checkers (D-36): each returns the stored form or raises `invalid_value` naming the field.
Pure functions - no database."""

import pytest

from studysystem.errors import StudyError
from studysystem.services import values


def test_text_strips_surrounding_spaces():
    assert values.text("code", "  I3302-E ") == "I3302-E"


@pytest.mark.parametrize("raw", ["", "   ", 5, None])
def test_text_rejects_empty_or_not_a_string(raw):
    with pytest.raises(StudyError) as excinfo:
        values.text("code", raw)
    assert excinfo.value.code == "invalid_value"
    assert [e["field"] for e in excinfo.value.field_errors] == ["code"]


@pytest.mark.parametrize(
    ("raw", "stored"),
    [(3, 3.0), (3.5, 3.5), ("3", 3.0), (" 3.5 ", 3.5)],
)
def test_number_takes_a_number_or_a_string_that_reads_as_one(raw, stored):
    assert values.number("credits", raw, gt=0) == stored


def test_number_bounds_are_inclusive_or_strict_as_named():
    assert values.number("weight", 0, ge=0, le=100) == 0.0
    assert values.number("weight", 100, ge=0, le=100) == 100.0


@pytest.mark.parametrize(
    ("raw", "bounds"),
    [
        (0, {"gt": 0}),  # credits 0: above 0 is strict
        (-1, {"ge": 0}),
        (101, {"le": 100}),
        ("abc", {}),
        ("", {}),
        (True, {}),  # a bool is an int to Python, not a number to Mohammad
        ("nan", {"ge": 0, "le": 100}),  # float() reads it, and every comparison with it is False
        ("inf", {"gt": 0}),
    ],
)
def test_number_rejects(raw, bounds):
    with pytest.raises(StudyError) as excinfo:
        values.number("credits", raw, **bounds)
    assert excinfo.value.code == "invalid_value"
    assert [e["field"] for e in excinfo.value.field_errors] == ["credits"]


def test_day_hands_back_a_real_date_unchanged():
    assert values.day("date", "2027-01-18") == "2027-01-18"
    assert values.day("date", "2028-02-29") == "2028-02-29"  # a leap year


@pytest.mark.parametrize(
    "raw",
    [
        "2027-02-30",  # the right shape, not a real day
        "2027-02-29",  # not a leap year
        "2027-1-18",
        "20270118",  # fromisoformat alone would take these two
        "2027-W03-1",
        "18/01/2027",
        "2027-01",  # a month is day_or_month's job, not day's
        " 2027-01-18",
        20270118,
        "٢٠٢٧-٠١-١٨",  # Arabic-Indic digits
    ],
)
def test_day_rejects(raw):
    with pytest.raises(StudyError) as excinfo:
        values.day("date", raw)
    assert excinfo.value.code == "invalid_value"
    assert [e["field"] for e in excinfo.value.field_errors] == ["date"]


@pytest.mark.parametrize(
    ("raw", "stored"),
    [
        ("2027-01-18", ("2027-01-18", False)),
        ("2027-01", ("2027-01-01", True)),
        ("2027-12", ("2027-12-01", True)),
    ],
)
def test_day_or_month_exact_or_first_of_month(raw, stored):
    assert values.day_or_month("date", raw) == stored


@pytest.mark.parametrize(
    "raw",
    ["2027-13", "2027-00", "2027-1", "January 2027", "2027-02-30", 202701, None],
)
def test_day_or_month_rejects(raw):
    with pytest.raises(StudyError) as excinfo:
        values.day_or_month("date", raw)
    assert excinfo.value.code == "invalid_value"
    assert [e["field"] for e in excinfo.value.field_errors] == ["date"]


@pytest.mark.parametrize("raw", ["09:00", "00:00", "23:59"])
def test_clock_hands_back_a_real_time_unchanged(raw):
    assert values.clock("time", raw) == raw


@pytest.mark.parametrize(
    "raw", ["24:00", "12:60", "9:00", "9am", "09:00:00", "09.00", " 09:00", 900, None]
)
def test_clock_rejects(raw):
    with pytest.raises(StudyError) as excinfo:
        values.clock("time", raw)
    assert excinfo.value.code == "invalid_value"
    assert [e["field"] for e in excinfo.value.field_errors] == ["time"]
