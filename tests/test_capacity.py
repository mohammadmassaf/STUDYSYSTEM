"""Capacity (D-17): insert-only, one dated row per call - a later row wins from its date on, and
never overwrites the one before it."""

import pytest
from sqlalchemy import select

from studysystem.db.tables import capacity
from studysystem.errors import StudyError
from studysystem.services.capacity import set_capacity


def rows(engine) -> list:
    with engine.connect() as conn:
        return conn.execute(select(capacity).order_by(capacity.c.id)).all()


def test_one_call_writes_one_row(service_engine, user_id):
    result = set_capacity(service_engine, user_id, 20, "2026-10-01")

    [row] = rows(service_engine)
    assert result == {"capacity_id": row.id, "hours_per_week": 20, "from_date": "2026-10-01"}
    assert (row.user_id, row.hours_per_week, row.from_date) == (user_id, 20, "2026-10-01")


def test_hours_as_a_numeric_string_are_stored_as_a_number(service_engine, user_id):
    set_capacity(service_engine, user_id, "20", "2026-10-01")

    assert rows(service_engine)[0].hours_per_week == 20.0


def test_a_second_call_adds_a_row_and_keeps_the_first(service_engine, user_id):
    """A vacation: its start, then the return - both rows stay, in order."""
    set_capacity(service_engine, user_id, 20, "2026-10-01")
    set_capacity(service_engine, user_id, 40, "2027-02-01")

    assert [(r.hours_per_week, r.from_date) for r in rows(service_engine)] == [
        (20, "2026-10-01"),
        (40, "2027-02-01"),
    ]


def test_the_upper_bound_is_allowed(service_engine, user_id):
    set_capacity(service_engine, user_id, 112, "2026-10-01")  # 16 a day, the CHECK's limit

    assert rows(service_engine)[0].hours_per_week == 112


@pytest.mark.parametrize(
    ("hours", "from_date", "field"),
    [
        (0, "2026-10-01", "hours_per_week"),
        (-5, "2026-10-01", "hours_per_week"),
        (113, "2026-10-01", "hours_per_week"),
        ("lots", "2026-10-01", "hours_per_week"),
        (20, "2026-02-30", "from_date"),
        (20, "2026-10", "from_date"),  # an exact day only - no month form here
        (20, "01/10/2026", "from_date"),
    ],
)
def test_a_bad_input_is_invalid_and_writes_nothing(
    service_engine, user_id, hours, from_date, field
):
    with pytest.raises(StudyError) as excinfo:
        set_capacity(service_engine, user_id, hours, from_date)

    assert excinfo.value.code == "invalid_value"
    assert excinfo.value.field_errors[0]["field"] == field
    assert rows(service_engine) == []
