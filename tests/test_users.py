"""The one v1 user (D-31): `study migrate` creates it, `current_user()` only reads it."""

import pytest
from sqlalchemy import insert, select

from studysystem.db.tables import user
from studysystem.errors import StudyError
from studysystem.services.ids import new_id, now
from studysystem.services.users import current_user, ensure_user


def test_no_user_names_the_command_that_makes_one(service_engine):
    with pytest.raises(StudyError) as excinfo:
        current_user(service_engine)
    assert excinfo.value.code == "no_user"
    assert "study migrate" in excinfo.value.fix


def test_ensure_user_twice_makes_one_row_and_returns_the_same_id(service_engine):
    first = ensure_user(service_engine)
    second = ensure_user(service_engine)
    assert first == second
    with service_engine.connect() as conn:
        assert conn.execute(select(user.c.id)).scalars().all() == [first]


def test_current_user_returns_the_id_ensure_user_made(service_engine):
    user_id = ensure_user(service_engine)
    assert current_user(service_engine) == user_id


def test_two_users_is_an_error_not_a_pick(service_engine):
    ensure_user(service_engine)
    with service_engine.begin() as conn:  # only reachable by writing around `study migrate`
        conn.execute(insert(user).values(id=new_id(), created_at=now()))

    with pytest.raises(StudyError) as excinfo:
        current_user(service_engine)
    assert excinfo.value.code == "too_many_users"
