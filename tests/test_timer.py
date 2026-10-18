"""The dashboard timer's stop (3.5; D-17, D-198): the page counts, the server's clock caps the
count, and a session past its 4 hours records nothing - on both engines. Each test is one evening
on I3304's IP addressing, or I3302's first-material setup item."""

import datetime

import pytest
from sqlalchemy import func, insert, select

from studysystem.db.tables import hours_entry, study_session, topic
from studysystem.errors import StudyError
from studysystem.services import study_loop
from studysystem.services.courses import add_course
from studysystem.services.ids import new_id, now
from studysystem.services.plan import get_plan
from studysystem.services.study_loop import log_hours, start_session, stop_timer
from studysystem.services.units import write_unit

SEM = "Semester 1 2026-2027"
TODAY = datetime.date(2026, 10, 7)


@pytest.fixture
def nets(service_engine, user_id):
    return add_course(service_engine, user_id, "I3304", "Network Administration and Security", SEM)


@pytest.fixture
def ip(service_engine, user_id, nets):
    topic_id = new_id()
    with write_unit(service_engine) as conn:
        conn.execute(
            insert(topic).values(
                id=topic_id,
                user_id=user_id,
                course_id=nets["course_id"],
                name="IP addressing & subnetting",
                status="active",
                proposed_by="profile",
                status_changed_at=now(),
                created_at=now(),
            )
        )
    return topic_id


@pytest.fixture
def session(service_engine, user_id, ip):
    """Practice on IP addressing, started now - "19:00" in the docstring's examples."""
    return start_session(service_engine, user_id, "I3304", "practice", "topic", ip)


@pytest.fixture
def stop_at(monkeypatch, session):
    """Sets the loop's clock to the session's start plus a span; returns that stamp."""
    start = datetime.datetime.fromisoformat(session["started_at"])

    def at(**span) -> str:
        stamp = (start + datetime.timedelta(**span)).strftime(study_loop.TIMESTAMP)
        monkeypatch.setattr(study_loop, "now", lambda: stamp)
        return stamp

    return at


def _entries(engine) -> list:
    with engine.connect() as conn:
        return conn.execute(select(hours_entry)).all()


def _ended(engine, session_id):
    with engine.connect() as conn:
        return conn.execute(
            select(study_session.c.ended_at).where(study_session.c.id == session_id)
        ).scalar_one()


def _stop(engine, uid, session, minutes, **kw):
    return stop_timer(engine, uid, "I3304", session["session_id"], minutes, **kw)


# --- a stop that counts --------------------------------------------------------------------


def test_a_stopped_timer_logs_the_pages_minutes_as_timer(
    service_engine, user_id, ip, session, stop_at
):
    stamp = stop_at(minutes=52)

    result = _stop(service_engine, user_id, session, 52)

    [entry] = _entries(service_engine)
    assert (entry.minutes, entry.topic_id, entry.setup_key, entry.source) == (52, ip, None, "timer")
    assert (entry.session_id, entry.occurred_at) == (session["session_id"], session["started_at"])
    assert _ended(service_engine, session["session_id"]) == stamp
    assert result == {
        "hours_entry_id": entry.id,
        "minutes": 52,
        "session_id": session["session_id"],
        "ended_at": stamp,
    }


def test_a_paused_session_logs_the_pages_count_not_the_clock(
    service_engine, user_id, session, stop_at
):
    stop_at(minutes=70)  # 50 minutes of work, 20 paused for dinner

    _stop(service_engine, user_id, session, 50)

    assert [e.minutes for e in _entries(service_engine)] == [50]


def test_the_clock_rounds_up_so_a_second_of_drift_is_no_refusal(
    service_engine, user_id, session, stop_at
):
    stop_at(minutes=52, seconds=10)

    _stop(service_engine, user_id, session, 53)

    assert [e.minutes for e in _entries(service_engine)] == [53]


def test_under_a_minute_ends_the_session_and_logs_nothing(
    service_engine, user_id, session, stop_at
):
    stamp = stop_at(seconds=20)

    result = _stop(service_engine, user_id, session, 0)

    assert result["hours_entry_id"] is None
    assert _entries(service_engine) == []
    assert _ended(service_engine, session["session_id"]) == stamp


def test_a_setup_item_logs_on_its_key_with_no_topic(service_engine, user_id, monkeypatch):
    add_course(service_engine, user_id, "I3302", "Server-side WEB Development", SEM)
    items = get_plan(service_engine, user_id, TODAY)["lanes"]["setup"]["items"]
    key = next(i["setup_key"] for i in items if i["setup_kind"] == "first-material")
    session = start_session(service_engine, user_id, "I3302", "explain-chapter")
    start = datetime.datetime.fromisoformat(session["started_at"])
    stamp = (start + datetime.timedelta(minutes=30)).strftime(study_loop.TIMESTAMP)
    monkeypatch.setattr(study_loop, "now", lambda: stamp)

    stop_timer(service_engine, user_id, "I3302", session["session_id"], 30, setup_key=key)

    [entry] = _entries(service_engine)
    assert (entry.minutes, entry.topic_id, entry.setup_key, entry.source) == (
        30,
        None,
        key,
        "timer",
    )


# --- AFK: the half of Done-when that writes nothing ----------------------------------------


def test_a_session_never_stopped_records_nothing(service_engine, session):
    assert _entries(service_engine) == []


@pytest.mark.parametrize("hours", [4, 6])
def test_a_stop_at_or_after_four_hours_is_refused_and_records_nothing(
    service_engine, user_id, session, stop_at, hours
):
    stop_at(hours=hours)

    with pytest.raises(StudyError) as err:
        _stop(service_engine, user_id, session, hours * 60)

    assert err.value.code == "session_expired"
    assert err.value.field_errors[0]["field"] == "session_id"
    assert "study_log_hours" in err.value.fix
    assert _entries(service_engine) == []
    assert _ended(service_engine, session["session_id"]) is None


# --- refusals that write nothing -----------------------------------------------------------


def test_more_minutes_than_have_passed_are_refused(service_engine, user_id, session, stop_at):
    stop_at(minutes=52)

    with pytest.raises(StudyError) as err:
        _stop(service_engine, user_id, session, 90)

    assert (err.value.code, err.value.field_errors[0]["field"]) == ("invalid_value", "minutes")
    assert _entries(service_engine) == []
    assert _ended(service_engine, session["session_id"]) is None


def test_a_second_stop_is_refused_and_the_first_entry_stands(
    service_engine, user_id, session, stop_at
):
    stop_at(minutes=52)
    _stop(service_engine, user_id, session, 52)
    stop_at(minutes=55)

    with pytest.raises(StudyError) as err:
        _stop(service_engine, user_id, session, 52)

    assert err.value.code == "session_ended"
    assert [e.minutes for e in _entries(service_engine)] == [52]


def test_a_session_closed_by_log_hours_cannot_be_stopped(service_engine, user_id, session, stop_at):
    log_hours(service_engine, user_id, "I3304", 40, session_id=session["session_id"])
    stop_at(minutes=52)

    with pytest.raises(StudyError) as err:
        _stop(service_engine, user_id, session, 52)

    assert err.value.code == "session_ended"
    assert [e.source for e in _entries(service_engine)] == ["tool"]


def test_a_setup_key_no_plan_raised_is_not_found(service_engine, user_id, monkeypatch, nets):
    session = start_session(service_engine, user_id, "I3304", "practice")
    start = datetime.datetime.fromisoformat(session["started_at"])
    stamp = (start + datetime.timedelta(minutes=10)).strftime(study_loop.TIMESTAMP)
    monkeypatch.setattr(study_loop, "now", lambda: stamp)

    with pytest.raises(StudyError) as err:
        _stop(service_engine, user_id, session, 10, setup_key="first-material:typo")

    assert (err.value.code, err.value.field_errors[0]["field"]) == ("not_found", "setup_key")
    assert _ended(service_engine, session["session_id"]) is None


def test_a_topic_session_takes_no_setup_key(service_engine, user_id, session, stop_at):
    stop_at(minutes=10)

    with pytest.raises(StudyError) as err:
        _stop(service_engine, user_id, session, 10, setup_key="first-material:x")

    assert (err.value.code, err.value.field_errors[0]["field"]) == ("invalid_value", "setup_key")
    assert _entries(service_engine) == []


def test_an_unknown_session_is_not_found(service_engine, user_id, nets):
    with pytest.raises(StudyError) as err:
        stop_timer(service_engine, user_id, "I3304", new_id(), 10)

    assert (err.value.code, err.value.field_errors[0]["field"]) == ("not_found", "session_id")


def test_a_session_of_another_course_is_not_found(service_engine, user_id, session, stop_at):
    add_course(service_engine, user_id, "I3302", "Server-side WEB Development", SEM)
    stop_at(minutes=10)

    with pytest.raises(StudyError) as err:
        stop_timer(service_engine, user_id, "I3302", session["session_id"], 10)

    assert (err.value.code, err.value.field_errors[0]["field"]) == ("not_found", "session_id")
    assert _ended(service_engine, session["session_id"]) is None


def test_another_students_session_cannot_be_stopped(
    service_engine, user_id, other_user_id, session, stop_at
):
    add_course(service_engine, other_user_id, "I3304", "Network Administration and Security", SEM)
    stop_at(minutes=10)

    with pytest.raises(StudyError) as err:
        _stop(service_engine, other_user_id, session, 10)

    assert (err.value.code, err.value.field_errors[0]["field"]) == ("not_found", "session_id")
    assert _ended(service_engine, session["session_id"]) is None


@pytest.mark.parametrize("minutes", [-1, 721, 40.5, "forty", None])
def test_minutes_are_whole_and_0_to_720(service_engine, user_id, session, minutes):
    with pytest.raises(StudyError) as err:
        _stop(service_engine, user_id, session, minutes)

    assert (err.value.code, err.value.field_errors[0]["field"]) == ("invalid_value", "minutes")
    with service_engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(hours_entry)).scalar_one() == 0
