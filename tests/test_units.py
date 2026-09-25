"""The write unit (D-33): a unit either lands whole or leaves nothing behind. Run on the runtime
SQLite file and on Postgres."""

import pytest
from sqlalchemy import Engine, func, insert, select
from sqlalchemy.exc import IntegrityError

from studysystem.db.tables import course, user
from studysystem.services.ids import new_id, now
from studysystem.services.units import write_unit


def count(engine: Engine, table) -> int:
    """Rows in `table`, read on a fresh connection - what any other process would see."""
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def i3302(user_id: str, **override) -> dict:
    """A valid course row; `override` changes the one field a test is about."""
    row = dict(
        id=new_id(),
        user_id=user_id,
        code="I3302",
        name="Databases",
        semester_name="Fall 2026",
        instructor_tier="unknown",
        credits=3,
        credits_tier="declared",
        created_at=now(),
    )
    return row | override


def test_a_unit_that_fails_halfway_leaves_no_rows(service_engine):
    # The second insert is rejected by the database itself (`credits > 0`), not a fake raise:
    # the first row was really written inside the transaction before the failure.
    user_id = new_id()
    with pytest.raises(IntegrityError):
        with write_unit(service_engine) as conn:
            conn.execute(insert(user).values(id=user_id, created_at=now()))
            conn.execute(insert(course).values(i3302(user_id, credits=0)))

    assert count(service_engine, user) == 0
    assert count(service_engine, course) == 0


def test_a_unit_that_finishes_commits_every_row(service_engine):
    user_id = new_id()
    with write_unit(service_engine) as conn:
        conn.execute(insert(user).values(id=user_id, created_at=now()))
        conn.execute(insert(course).values(i3302(user_id)))

    assert count(service_engine, user) == 1
    assert count(service_engine, course) == 1


def test_the_original_error_reaches_the_caller(service_engine):
    class Boom(Exception):
        pass

    raised = Boom("the service's own error")
    with pytest.raises(Boom) as excinfo:
        with write_unit(service_engine) as conn:
            conn.execute(insert(user).values(id=new_id(), created_at=now()))
            raise raised

    assert excinfo.value is raised  # not wrapped, not swallowed
    assert count(service_engine, user) == 0
