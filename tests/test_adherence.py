"""Adherence (4.7; D-12, D-117, D-273 - D-276) on both engines, and the reason field that feeds it.

Every week here is the week of Monday 2026-11-02 in Beirut, UTC+2 in November: it runs from
2026-11-01T22:00:00Z to 2026-11-08T22:00:00Z. Plans and hours carry fixed timestamps, shaped on
his live database: I3304 with its Ch3 Transport "(2)" chapter and the "(1)" copy beside it.
"""

import datetime

import pytest
from sqlalchemy import insert, select

from studysystem.db.tables import hours_entry, plan_item
from studysystem.errors import StudyError
from studysystem.services.adherence import _week_bounds, weekly_adherence
from studysystem.services.ids import new_id, now
from studysystem.services.study_loop import log_hours
from studysystem.services.units import read_unit, write_unit
from tests.test_gate import _course, _plan, _setup_item, _study_item, _topic
from tests.test_plan import _chapter

BEIRUT = datetime.timezone(datetime.timedelta(hours=2))  # November: no daylight saving
WEEK = datetime.date(2026, 11, 2)
NO_DEVIATIONS = {"disagreed": 0, "too-tired": 0, "topic-wrong": 0, "backlog": 0, "unexplained": 0}


@pytest.fixture
def nets(service_engine, user_id):
    return _course(service_engine, user_id, "I3304")


@pytest.fixture
def layers(service_engine, user_id, nets):
    return _topic(service_engine, user_id, nets, "Protocol layers (OSI / TCP-IP)")


@pytest.fixture
def plan_mon(service_engine, user_id):
    """A plan made Monday 2026-11-02 at 10:00 local."""
    return _plan(service_engine, user_id, "2026-11-02T08:00:00Z")


def _review_item(engine, uid, plan_id, course_id, topic_id, rank, shown):
    with write_unit(engine) as conn:
        conn.execute(
            insert(plan_item).values(
                id=new_id(),
                user_id=uid,
                plan_id=plan_id,
                lane="review",
                course_id=course_id,
                topic_id=topic_id,
                rank=rank,
                shown=int(shown),
                score=1.0,
                reason=f"review #{rank}",
                estimated_minutes=20,
            )
        )


def _hours(
    engine,
    uid,
    course_id,
    occurred_at,
    minutes=60,
    *,
    topic_id=None,
    setup_key=None,
    material_id=None,
    reason=None,
    voided=False,
):
    with write_unit(engine) as conn:
        conn.execute(
            insert(hours_entry).values(
                id=new_id(),
                user_id=uid,
                course_id=course_id,
                topic_id=topic_id,
                setup_key=setup_key,
                material_id=material_id,
                minutes=minutes,
                occurred_at=occurred_at,
                source="tool",
                deviation_reason=reason,
                voided_at="2026-11-07T21:00:00Z" if voided else None,
                created_at=now(),
            )
        )


def _week(engine, uid, week_start=WEEK):
    with read_unit(engine) as conn:
        return weekly_adherence(conn, uid, week_start, BEIRUT)


# --- the week's bounds ---------------------------------------------------------------------------


def test_the_week_runs_from_local_monday_midnight_in_utc():
    assert _week_bounds(WEEK, BEIRUT) == ("2026-11-01T22:00:00Z", "2026-11-08T22:00:00Z")


# --- in-plan or not -----------------------------------------------------------------------------


def test_an_hour_on_a_topic_the_plan_in_force_shows_is_in_plan(
    service_engine, user_id, nets, layers, plan_mon
):
    _study_item(service_engine, user_id, plan_mon, nets, layers, 1, True)
    _hours(service_engine, user_id, nets, "2026-11-03T17:00:00Z", topic_id=layers)

    assert _week(service_engine, user_id) == {
        "week_start": "2026-11-02",
        "minutes": 60,
        "in_plan": 60,
        "missing_input": 0,
        "adherence": 1.0,
        "deviations": NO_DEVIATIONS,
    }


def test_a_topic_shown_in_two_lanes_counts_its_minutes_once(
    service_engine, user_id, nets, layers, plan_mon
):
    # D-117: review #1 and study #1 of one plan - a join would count these 20 minutes twice.
    _review_item(service_engine, user_id, plan_mon, nets, layers, 1, True)
    _study_item(service_engine, user_id, plan_mon, nets, layers, 1, True)
    _hours(service_engine, user_id, nets, "2026-11-03T17:00:00Z", 20, topic_id=layers)

    week = _week(service_engine, user_id)

    assert (week["minutes"], week["in_plan"], week["adherence"]) == (20, 20, 1.0)


def test_a_chapter_hour_counts_on_the_shown_chapter_and_not_on_its_copy(
    service_engine, user_id, nets, layers, plan_mon
):
    # His Nov 5: the plan showed Ch3 Transport (2); one hour went on the "(1)" copy.
    with write_unit(service_engine) as conn:
        shown = _chapter(conn, user_id, nets, filename="Chapter 3- Transport layer (2).pdf")
        copy = _chapter(conn, user_id, nets, filename="Chapter 3- Transport layer (1).pdf")
    _study_item(service_engine, user_id, plan_mon, nets, layers, 1, True, shown)
    _hours(service_engine, user_id, nets, "2026-11-05T17:19:00Z", 50, material_id=copy)
    _hours(service_engine, user_id, nets, "2026-11-05T18:56:00Z", 50, material_id=shown)

    week = _week(service_engine, user_id)

    assert (week["minutes"], week["in_plan"], week["adherence"]) == (100, 50, 0.5)
    assert week["deviations"] == {**NO_DEVIATIONS, "unexplained": 50}


def test_an_hour_on_a_shown_setup_key_is_in_plan(service_engine, user_id, nets, plan_mon):
    _setup_item(service_engine, user_id, plan_mon, nets, 1, True)
    key = f"first-material:{nets}"
    _hours(service_engine, user_id, nets, "2026-11-03T17:00:00Z", 30, setup_key=key)

    assert _week(service_engine, user_id)["in_plan"] == 30


def test_a_topic_ranked_but_not_shown_is_off_plan(service_engine, user_id, nets, layers, plan_mon):
    _study_item(service_engine, user_id, plan_mon, nets, layers, 6, False)
    _hours(service_engine, user_id, nets, "2026-11-03T17:00:00Z", topic_id=layers)

    week = _week(service_engine, user_id)

    assert (week["in_plan"], week["adherence"]) == (0, 0.0)
    assert week["deviations"] == {**NO_DEVIATIONS, "unexplained": 60}


def test_a_course_level_hour_is_off_plan_and_unexplained(
    service_engine, user_id, nets, layers, plan_mon
):
    # His Nov 3: 60 minutes on I3303 with no topic, chapter or key - NULLs match no item.
    _study_item(service_engine, user_id, plan_mon, nets, layers, 1, True)
    _setup_item(service_engine, user_id, plan_mon, nets, 1, True)
    _hours(service_engine, user_id, nets, "2026-11-03T18:34:00Z")

    week = _week(service_engine, user_id)

    assert (week["minutes"], week["in_plan"]) == (60, 0)
    assert week["deviations"] == {**NO_DEVIATIONS, "unexplained": 60}


def test_only_the_newest_plan_before_the_hour_counts(
    service_engine, user_id, nets, layers, plan_mon
):
    # Monday's plan showed the topic; Wednesday's plan, made before the hour, does not.
    other = _topic(service_engine, user_id, nets, "IP addressing & subnetting")
    _study_item(service_engine, user_id, plan_mon, nets, layers, 1, True)
    plan_wed = _plan(service_engine, user_id, "2026-11-04T08:00:00Z")
    _study_item(service_engine, user_id, plan_wed, nets, other, 1, True)
    _hours(service_engine, user_id, nets, "2026-11-04T17:00:00Z", topic_id=layers)

    assert _week(service_engine, user_id)["in_plan"] == 0


def test_an_hour_before_any_plan_is_off_plan(service_engine, user_id, nets, layers, plan_mon):
    # Monday 10:00 local is the first plan; this hour started at 08:00 local.
    _study_item(service_engine, user_id, plan_mon, nets, layers, 1, True)
    _hours(service_engine, user_id, nets, "2026-11-02T06:00:00Z", topic_id=layers)

    week = _week(service_engine, user_id)

    assert (week["minutes"], week["in_plan"]) == (60, 0)


# --- reasons ------------------------------------------------------------------------------------


def test_a_backlog_hour_lands_in_its_own_category(service_engine, user_id, nets, plan_mon):
    _hours(service_engine, user_id, nets, "2026-11-03T17:00:00Z", 40, reason="backlog")

    week = _week(service_engine, user_id)

    assert week["deviations"] == {**NO_DEVIATIONS, "backlog": 40}
    assert (week["missing_input"], week["adherence"]) == (0, 0.0)


def test_an_external_deadline_is_a_missing_input_and_leaves_the_denominator(
    service_engine, user_id, nets, layers, plan_mon
):
    _study_item(service_engine, user_id, plan_mon, nets, layers, 1, True)
    _hours(service_engine, user_id, nets, "2026-11-03T17:00:00Z", topic_id=layers)
    _hours(service_engine, user_id, nets, "2026-11-04T17:00:00Z", reason="external-deadline")

    week = _week(service_engine, user_id)

    assert (week["minutes"], week["in_plan"], week["missing_input"]) == (120, 60, 60)
    assert week["adherence"] == 1.0  # 60 / (120 - 60), not 60 / 120
    assert week["deviations"] == NO_DEVIATIONS


def test_a_reason_on_an_in_plan_hour_is_ignored(service_engine, user_id, nets, layers, plan_mon):
    _study_item(service_engine, user_id, plan_mon, nets, layers, 1, True)
    at = "2026-11-03T17:00:00Z"
    _hours(service_engine, user_id, nets, at, topic_id=layers, reason="disagreed")

    week = _week(service_engine, user_id)

    assert (week["in_plan"], week["adherence"]) == (60, 1.0)
    assert week["deviations"] == NO_DEVIATIONS


# --- edges --------------------------------------------------------------------------------------


def test_a_voided_hour_never_counts(service_engine, user_id, nets, layers, plan_mon):
    _study_item(service_engine, user_id, plan_mon, nets, layers, 1, True)
    _hours(service_engine, user_id, nets, "2026-11-03T17:00:00Z", 45, topic_id=layers)
    _hours(service_engine, user_id, nets, "2026-11-03T18:00:00Z", 90, voided=True)

    week = _week(service_engine, user_id)

    assert (week["minutes"], week["in_plan"], week["adherence"]) == (45, 45, 1.0)


def test_a_week_with_no_hours_reads_unknown_not_zero(service_engine, user_id, nets, plan_mon):
    # His Oct 26 - Nov 1 shape: no hours logged.
    assert _week(service_engine, user_id) == {
        "week_start": "2026-11-02",
        "minutes": 0,
        "in_plan": 0,
        "missing_input": 0,
        "adherence": None,
        "deviations": NO_DEVIATIONS,
    }


def test_a_week_of_only_missing_inputs_reads_unknown_not_zero(
    service_engine, user_id, nets, plan_mon
):
    # 4.7's Done-when, on his Oct 12-18 shape: five hours, 200 minutes, all for a deadline.
    for day, minutes in ((2, 40), (3, 45), (6, 60), (6, 15), (7, 40)):
        at = f"2026-11-0{day}T{10 + minutes // 5:02d}:00:00Z"
        _hours(service_engine, user_id, nets, at, minutes, reason="external-deadline")

    week = _week(service_engine, user_id)

    assert (week["minutes"], week["missing_input"], week["adherence"]) == (200, 200, None)
    assert week["deviations"] == NO_DEVIATIONS


def test_the_week_ends_at_local_midnight_not_at_utc_midnight(
    service_engine, user_id, nets, plan_mon
):
    _hours(service_engine, user_id, nets, "2026-11-01T22:00:00Z", 10)  # Mon 00:00 local: in
    _hours(service_engine, user_id, nets, "2026-11-08T21:30:00Z", 20)  # Sun 23:30 local: in
    _hours(service_engine, user_id, nets, "2026-11-08T22:30:00Z", 40)  # Mon 00:30 local: next

    assert _week(service_engine, user_id)["minutes"] == 30
    assert _week(service_engine, user_id, datetime.date(2026, 11, 9))["minutes"] == 40


# --- the reason field on study_log_hours (D-276) ------------------------------------------------


def _stored_reasons(engine) -> list:
    with engine.connect() as conn:
        query = select(hours_entry.c.deviation_reason).order_by(hours_entry.c.id)
        return conn.execute(query).scalars().all()


def test_logged_hours_keep_the_reason_they_were_given(service_engine, user_id, nets):
    log_hours(service_engine, user_id, "I3304", 40, deviation_reason=" Backlog ")

    assert _stored_reasons(service_engine) == ["backlog"]


def test_logged_hours_without_a_reason_store_none(service_engine, user_id, nets):
    # `unexplained` is read from the NULL when a week is scored, not written here (D-276).
    log_hours(service_engine, user_id, "I3304", 40)

    assert _stored_reasons(service_engine) == [None]


def test_a_reason_outside_the_six_is_refused_and_nothing_is_written(service_engine, user_id, nets):
    with pytest.raises(StudyError) as excinfo:
        log_hours(service_engine, user_id, "I3304", 40, deviation_reason="bored")

    assert excinfo.value.code == "invalid_value"
    assert excinfo.value.field_errors[0]["field"] == "deviation_reason"
    assert _stored_reasons(service_engine) == []


def test_a_reason_on_a_void_alone_is_refused(service_engine, user_id, nets):
    entry = log_hours(service_engine, user_id, "I3304", 40)["hours_entry_id"]

    with pytest.raises(StudyError) as excinfo:
        log_hours(service_engine, user_id, "I3304", voids=entry, deviation_reason="too-tired")

    assert excinfo.value.field_errors[0]["field"] == "deviation_reason"
    with service_engine.connect() as conn:
        assert conn.execute(select(hours_entry.c.voided_at)).scalar_one() is None  # not voided


def test_a_late_reason_is_a_void_plus_the_entry_again(service_engine, user_id, nets):
    entry = log_hours(service_engine, user_id, "I3304", 40)["hours_entry_id"]

    log_hours(service_engine, user_id, "I3304", 40, voids=entry, deviation_reason="topic-wrong")

    with service_engine.connect() as conn:
        live = conn.execute(
            select(hours_entry.c.minutes, hours_entry.c.deviation_reason).where(
                hours_entry.c.voided_at.is_(None)
            )
        ).all()
    assert [tuple(r) for r in live] == [(40, "topic-wrong")]


# --- the in_plan reply (D-276) ------------------------------------------------------------------


def test_the_reply_says_in_plan_for_an_hour_on_a_shown_topic(
    service_engine, user_id, nets, layers, plan_mon
):
    _study_item(service_engine, user_id, plan_mon, nets, layers, 1, True)

    result = log_hours(service_engine, user_id, "I3304", 30, topic_id=layers)

    assert result["in_plan"] is True


def test_the_reply_says_not_in_plan_for_a_course_level_hour(
    service_engine, user_id, nets, layers, plan_mon
):
    # The host's cue to ask why - the reason is optional (D-12).
    _study_item(service_engine, user_id, plan_mon, nets, layers, 1, True)

    result = log_hours(service_engine, user_id, "I3304", 30)

    assert result["in_plan"] is False


# --- GET /state's metrics (D-163) ---------------------------------------------------------------


def test_state_sends_this_weeks_adherence_as_a_tiered_pair(
    service_engine, user_id, nets, layers, plan_mon
):
    from studysystem.services.state import get_state

    _study_item(service_engine, user_id, plan_mon, nets, layers, 1, True)
    _hours(service_engine, user_id, nets, "2026-11-03T10:00:00Z", 30, topic_id=layers)
    _hours(service_engine, user_id, nets, "2026-11-03T12:00:00Z", 10, reason="backlog")

    metrics = get_state(service_engine, user_id, datetime.date(2026, 11, 4), "test")["metrics"]
    del metrics["guess_sensitivity"]  # 4.8, tested in test_state.py

    assert metrics == {
        "adherence": {
            "week_start": "2026-11-02",
            "minutes": 40,
            "in_plan": 30,
            "missing_input": 0,
            "deviations": {**NO_DEVIATIONS, "backlog": 10},
            "adherence": 0.75,
            "adherence_tier": "inferred",
        }
    }


def test_state_sends_a_week_with_no_hours_as_unknown(service_engine, user_id):
    from studysystem.services.state import get_state

    adherence = get_state(service_engine, user_id, WEEK, "test")["metrics"]["adherence"]

    assert (adherence["adherence"], adherence["adherence_tier"]) == (None, "unknown")


def test_a_late_reason_keeps_the_voided_entrys_start(service_engine, user_id, nets):
    # D-277: his Nov 11 I3302 hour, re-logged days later, must not move to the re-log's week.
    from sqlalchemy import update

    entry = log_hours(service_engine, user_id, "I3304", 70)["hours_entry_id"]
    with write_unit(service_engine) as conn:
        conn.execute(
            update(hours_entry)
            .where(hours_entry.c.id == entry)
            .values(occurred_at="2026-11-11T18:02:54Z")
        )

    new = log_hours(
        service_engine, user_id, "I3304", 70, voids=entry, deviation_reason="external-deadline"
    )["hours_entry_id"]

    with service_engine.connect() as conn:
        start = conn.execute(
            select(hours_entry.c.occurred_at).where(hours_entry.c.id == new)
        ).scalar_one()
    assert start == "2026-11-11T18:02:54Z"


# --- started_at moves the start across the plan's moment (D-307) --------------------------------


def _local_text(utc: datetime.datetime) -> str:
    """A UTC moment as `started_at` text on this machine's clock (the zone log_hours reads)."""
    return utc.astimezone().strftime("%Y-%m-%dT%H:%M")


def test_in_plan_reads_the_started_at_start(service_engine, user_id, nets, layers, plan_mon):
    # plan_mon is made at 2026-11-02T08:00:00Z. An hour started before it saw no plan; one
    # started after it did.
    _study_item(service_engine, user_id, plan_mon, nets, layers, 1, True)
    plan_at = datetime.datetime(2026, 11, 2, 8, 0, tzinfo=datetime.UTC)

    before = log_hours(
        service_engine,
        user_id,
        "I3304",
        30,
        topic_id=layers,
        started_at=_local_text(plan_at - datetime.timedelta(hours=1)),
    )
    after = log_hours(
        service_engine,
        user_id,
        "I3304",
        30,
        topic_id=layers,
        started_at=_local_text(plan_at + datetime.timedelta(hours=1)),
    )

    assert before["in_plan"] is False
    assert after["in_plan"] is True
