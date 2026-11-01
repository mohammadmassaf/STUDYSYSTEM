"""The timetable task (3.6; D-19, D-216): his week leaves as a task and comes back as meetings read
from the image. Each meeting is judged alone and lands as a `class_meeting` row with tier
`inferred`, a pattern of its own (every week or an odd/even ISO week; the whole class or one lab
group), and the task it came from. The meetings' hours become one pending `capacity_suggestion`
(class hours x 0.5); accepting it writes a new `capacity` row beside the declared ones."""

import pytest
from sqlalchemy import insert, select
from sqlalchemy.exc import IntegrityError

from studysystem.db.tables import capacity, capacity_suggestion, class_meeting
from studysystem.errors import StudyError
from studysystem.services.capacity import accept_capacity_suggestion, set_capacity
from studysystem.services.courses import add_course, drop_course
from studysystem.services.generation import start_generation, submit_generation
from studysystem.services.ids import new_id, now
from studysystem.services.units import write_unit

SEM = "Semester 1 2026-2027"


def meeting(code, weekday, start, end, kind="lecture", **extra):
    """One meeting as the host reads it off the image."""
    return {
        "course_code": code,
        "weekday": weekday,
        "start_time": start,
        "end_time": end,
        "kind": kind,
    } | extra


def submit(engine, user_id, *meetings):
    """Start a timetable task and submit these meetings on it. Returns (task_id, reply)."""
    task = start_generation(engine, user_id, "timetable")
    task_id = task.payload["task_id"]
    return task_id, submit_generation(engine, user_id, task_id, {"meetings": list(meetings)})


def meetings_in(engine) -> list:
    with engine.connect() as conn:
        return conn.execute(
            select(class_meeting).order_by(
                class_meeting.c.weekday, class_meeting.c.start_time, class_meeting.c.id
            )
        ).all()


def suggestions(engine) -> list:
    with engine.connect() as conn:
        return conn.execute(select(capacity_suggestion)).all()


def capacities(engine) -> list:
    with engine.connect() as conn:
        return conn.execute(select(capacity).order_by(capacity.c.id)).all()


@pytest.fixture
def courses(service_engine, user_id) -> dict[str, str]:
    """{code: course_id} for the three courses these cases use."""
    return {
        code: add_course(service_engine, user_id, code, name, SEM)["course_id"]
        for code, name in [
            ("I3302", "Server-Side Web Development"),
            ("I3303", "Software Engineering"),
            ("I3305", "Mobile Development"),
        ]
    }


# --- 1. a meeting with no kind -----------------------------------------------------------------


def test_a_meeting_with_no_kind_is_refused_and_names_the_row(service_engine, user_id, courses):
    """The image carries no kind, so the host must ask him; the server never guesses one."""
    no_kind = meeting("I3303", 3, "10:00", "12:00")
    del no_kind["kind"]

    _, reply = submit(service_engine, user_id, meeting("I3302", 2, "08:00", "10:00"), no_kind)

    assert reply["accepted"] == [1]  # the good meeting still lands (D-52)
    [error] = reply["item_errors"]
    assert (error["item_ordinal"], error["field"]) == (2, "kind")
    [row] = meetings_in(service_engine)
    assert row.course_id == courses["I3302"]


# --- 2. two whole-class rows on one slot -------------------------------------------------------


def _raw_meeting(user_id, course_id, **patch) -> dict:
    return {
        "id": new_id(),
        "user_id": user_id,
        "course_id": course_id,
        "weekday": 2,
        "start_time": "09:00",
        "end_time": "11:00",
        "kind": "lecture",
        "tier": "declared",
        "week_parity": None,
        "group_label": None,
        "created_at": now(),
    } | patch


def test_two_whole_class_rows_on_one_slot_collide(service_engine, user_id, courses):
    """Both new columns are NULL on a whole-class row. Two NULLs never equal each other in a
    plain unique key - on SQLite or Postgres - so the key has to treat them as equal."""
    with write_unit(service_engine) as conn:
        conn.execute(insert(class_meeting).values(_raw_meeting(user_id, courses["I3302"])))

    with pytest.raises(IntegrityError):
        with write_unit(service_engine) as conn:
            conn.execute(insert(class_meeting).values(_raw_meeting(user_id, courses["I3302"])))

    assert len(meetings_in(service_engine)) == 1


def test_the_same_whole_class_slot_sent_twice_keeps_one_row(service_engine, user_id, courses):
    _, reply = submit(
        service_engine,
        user_id,
        meeting("I3302", 2, "09:00", "11:00"),
        meeting("I3302", 2, "09:00", "11:00"),
    )

    assert reply["accepted"] == [1]
    [error] = reply["item_errors"]
    assert error["item_ordinal"] == 2
    assert len(meetings_in(service_engine)) == 1


# --- 3. an alternating pair on one slot --------------------------------------------------------


def test_two_courses_alternating_in_one_slot_are_both_kept(service_engine, user_id, courses):
    """I3302 and I3303 share Tuesday 10:00-12:00, one in odd ISO weeks, one in even."""
    _, reply = submit(
        service_engine,
        user_id,
        meeting("I3302", 2, "10:00", "12:00", week_parity="odd"),
        meeting("I3303", 2, "10:00", "12:00", week_parity="even"),
    )

    assert reply["accepted"] == [1, 2]
    assert reply["item_errors"] == []
    assert {(r.course_id, r.week_parity) for r in meetings_in(service_engine)} == {
        (courses["I3302"], "odd"),
        (courses["I3303"], "even"),
    }


def test_one_course_in_one_slot_in_odd_and_in_even_weeks_is_two_rows(
    service_engine, user_id, courses
):
    """Same course, day, time and kind: only the week pattern tells them apart."""
    _, reply = submit(
        service_engine,
        user_id,
        meeting("I3302", 4, "08:00", "10:00", "lab", room="L1", week_parity="odd"),
        meeting("I3302", 4, "08:00", "10:00", "lab", room="L2", week_parity="even"),
    )

    assert reply["accepted"] == [1, 2]
    assert {r.room for r in meetings_in(service_engine)} == {"L1", "L2"}


def test_a_parity_other_than_odd_or_even_is_refused(service_engine, user_id, courses):
    """Parity is the ISO week number's (D-216); the host converts 'week A' before it submits."""
    _, reply = submit(
        service_engine, user_id, meeting("I3302", 2, "10:00", "12:00", week_parity="A")
    )

    assert reply["accepted"] == []
    assert (reply["item_errors"][0]["item_ordinal"], reply["item_errors"][0]["field"]) == (
        1,
        "week_parity",
    )
    assert meetings_in(service_engine) == []


# --- 4. a lab split into groups ----------------------------------------------------------------


def test_one_lab_slot_split_into_three_groups_keeps_all_three(service_engine, user_id, courses):
    """I3305's Wednesday lab runs as G1, G2 and G3 in the same hour."""
    _, reply = submit(
        service_engine,
        user_id,
        *[
            meeting("I3305", 3, "14:00", "16:00", "lab", group_label=g, room=f"Lab {n}")
            for n, g in enumerate(["G1", "G2", "G3"], start=1)
        ],
    )

    assert reply["accepted"] == [1, 2, 3]
    assert sorted(r.group_label for r in meetings_in(service_engine)) == ["G1", "G2", "G3"]


def test_a_whole_class_meeting_has_no_parity_and_no_group(service_engine, user_id, courses):
    """Unknown stays unknown: a missing pattern is NULL, not 'every' or 'all'."""
    submit(service_engine, user_id, meeting("I3302", 2, "09:00", "11:00"))

    [row] = meetings_in(service_engine)
    assert (row.week_parity, row.group_label) == (None, None)


def test_a_meeting_lands_inferred_and_points_at_its_task(service_engine, user_id, courses):
    task_id, _ = submit(service_engine, user_id, meeting("I3302", 2, "09:00", "11:00", room="B12"))

    [row] = meetings_in(service_engine)
    assert (row.tier, row.source_task_id, row.room, row.user_id) == (
        "inferred",
        task_id,
        "B12",
        user_id,
    )


# --- 5. a course code that matches no course ---------------------------------------------------


def test_an_unknown_course_code_is_returned_and_nothing_is_written_for_it(
    service_engine, user_id, courses
):
    """English is on his timetable but is not a course yet. The host asks him; the server only
    says which code and what to do (D-216)."""
    _, reply = submit(
        service_engine,
        user_id,
        meeting("I3302", 2, "09:00", "11:00"),
        meeting("ENG", 5, "12:00", "14:00"),
    )

    assert reply["accepted"] == [1]
    [error] = reply["item_errors"]
    assert (error["item_ordinal"], error["code"], error["field"]) == (
        2,
        "unknown_course",
        "course_code",
    )
    assert "ENG" in error["message"]
    assert [r.course_id for r in meetings_in(service_engine)] == [courses["I3302"]]


def test_the_codes_the_host_may_use_travel_with_the_task(service_engine, user_id, courses):
    task = start_generation(service_engine, user_id, "timetable")

    sent = {c["code"] for c in task.payload["rules"]["courses"]}
    assert sent == {"I3302", "I3303", "I3305"}


# --- 6. the suggested study hours --------------------------------------------------------------


def _twenty_class_hours() -> list[dict]:
    """Ten two-hour whole-class meetings: Monday to Friday, at 08:00 and at 10:00."""
    return [
        meeting("I3302", day, start, end)
        for day in range(1, 6)
        for start, end in [("08:00", "10:00"), ("10:00", "12:00")]
    ]


def test_twenty_class_hours_suggest_ten_study_hours(service_engine, user_id, courses):
    task_id, _ = submit(service_engine, user_id, *_twenty_class_hours())

    [row] = suggestions(service_engine)
    assert (row.hours_per_week, row.status, row.source_task_id, row.user_id) == (
        10.0,
        "pending",
        task_id,
        user_id,
    )
    assert row.resolved_at is None


def test_minutes_count_not_just_whole_hours(service_engine, user_id, courses):
    """90 minutes x 4 = 6 class hours -> 3."""
    submit(
        service_engine,
        user_id,
        *[meeting("I3302", day, "09:00", "10:30") for day in (1, 2, 3, 4)],
    )

    assert suggestions(service_engine)[0].hours_per_week == 3.0


def test_the_suggestion_leaves_the_declared_capacity_alone(service_engine, user_id, courses):
    set_capacity(service_engine, user_id, 10, "2026-09-27")

    submit(service_engine, user_id, *_twenty_class_hours())

    assert [r.hours_per_week for r in capacities(service_engine)] == [10]


def test_only_lectures_count_toward_the_suggestion(service_engine, user_id, courses):
    """Labs and tutorials are not class time that asks for study at home (D-216): 4 lecture hours
    plus a 3-hour lab and a 2-hour tutorial suggest 2, not 4.5."""
    submit(
        service_engine,
        user_id,
        meeting("I3302", 1, "08:00", "10:00"),
        meeting("I3302", 3, "08:00", "10:00"),
        meeting("I3305", 4, "14:00", "17:00", "lab"),
        meeting("I3303", 5, "10:00", "12:00", "tutorial"),
    )

    assert suggestions(service_engine)[0].hours_per_week == 2.0


def test_an_alternating_week_lecture_counts_in_full(service_engine, user_id, courses):
    """D-216: "count full in its week" - an odd- or even-week lecture counts as if it met every
    week. I3303 odd and I3302 even share Wednesday 08:00-10:00: 2 + 2 lecture hours suggest 2,
    not 1."""
    submit(
        service_engine,
        user_id,
        meeting("I3303", 3, "08:00", "10:00", week_parity="odd"),
        meeting("I3302", 3, "08:00", "10:00", week_parity="even"),
    )

    assert suggestions(service_engine)[0].hours_per_week == 2.0


def test_the_state_names_the_pattern_with_the_columns_names(service_engine, user_id, courses):
    """D-217: GET /state sends week_parity and group_label, NULL as "every" and "" - a known
    fact, never a bare null."""
    import datetime

    from studysystem.services.state import get_state

    submit(
        service_engine,
        user_id,
        meeting("I3302", 1, "08:00", "10:00"),
        meeting("I3303", 3, "08:00", "10:00", week_parity="odd"),
        meeting("I3305", 4, "14:00", "16:00", "lab", group_label="G2"),
    )

    sent = get_state(service_engine, user_id, datetime.date(2026, 11, 2), "test")
    assert {
        (m["code"], m["week_parity"], m["group_label"]) for m in sent["timetable"]["meetings"]
    } == {
        ("I3302", "every", ""),
        ("I3303", "odd", ""),
        ("I3305", "every", "G2"),
    }


def test_a_course_left_out_of_the_plan_counts_no_hours(service_engine, user_id, courses):
    """English is on his week but asks for no study (D-216): its lecture is kept as a meeting
    and left out of the suggestion. 4 lecture hours of I3302 plus 2 of ENG suggest 2."""
    add_course(service_engine, user_id, "ENG", "English", SEM, planned=False)

    submit(
        service_engine,
        user_id,
        meeting("I3302", 1, "08:00", "10:00"),
        meeting("I3302", 3, "08:00", "10:00"),
        meeting("ENG", 5, "12:00", "14:00"),
    )

    assert len(meetings_in(service_engine)) == 3
    assert suggestions(service_engine)[0].hours_per_week == 2.0


def test_a_later_submission_replaces_the_pending_suggestion(service_engine, user_id, courses):
    """One pending suggestion, counted from his whole week (D-216): the first
    submission keeps 18 lecture hours (9), the fixed lecture adds 2 more (10)."""
    no_kind = meeting("I3302", 5, "14:00", "16:00")
    del no_kind["kind"]
    task_id, first = submit(service_engine, user_id, *_twenty_class_hours()[:9], no_kind)
    assert first["suggested_hours_per_week"] == 9.0

    second = submit_generation(
        service_engine, user_id, task_id, {"meetings": [meeting("I3302", 5, "14:00", "16:00")]}
    )

    assert second["suggested_hours_per_week"] == 10.0
    assert sorted((r.hours_per_week, r.status) for r in suggestions(service_engine)) == [
        (9.0, "dismissed"),
        (10.0, "pending"),
    ]


def test_a_timetable_of_only_labs_suggests_nothing(service_engine, user_id, courses):
    """No lecture hours is unknown, not 0 - the column refuses 0 anyway."""
    submit(service_engine, user_id, meeting("I3305", 4, "14:00", "17:00", "lab"))

    assert len(meetings_in(service_engine)) == 1  # the lab is still on his week
    assert suggestions(service_engine) == []


def test_no_meeting_kept_means_no_suggestion(service_engine, user_id, courses):
    """Unknown stays unknown: a timetable with nothing valid suggests nothing, not 0 hours."""
    submit(service_engine, user_id, meeting("ENG", 5, "12:00", "14:00"))

    assert suggestions(service_engine) == []


# --- 7. accepting the suggestion ---------------------------------------------------------------


def _pending(engine, user_id) -> str:
    return suggestions(engine)[0].id


def test_accepting_writes_a_new_capacity_row_and_resolves_the_suggestion(
    service_engine, user_id, courses
):
    set_capacity(service_engine, user_id, 10, "2026-09-27")
    submit(service_engine, user_id, *_twenty_class_hours())

    result = accept_capacity_suggestion(
        service_engine, user_id, _pending(service_engine, user_id), "2026-11-02"
    )

    assert result["hours_per_week"] == 10.0
    assert result["from_date"] == "2026-11-02"
    assert [(r.hours_per_week, r.from_date) for r in capacities(service_engine)] == [
        (10, "2026-09-27"),  # the declared row stays
        (10, "2026-11-02"),
    ]
    [row] = suggestions(service_engine)
    assert row.status == "accepted"
    assert row.resolved_at is not None


def test_a_bad_date_writes_nothing_and_leaves_the_suggestion_pending(
    service_engine, user_id, courses
):
    submit(service_engine, user_id, *_twenty_class_hours())

    with pytest.raises(StudyError) as excinfo:
        accept_capacity_suggestion(
            service_engine, user_id, _pending(service_engine, user_id), "2026-02-30"
        )

    assert excinfo.value.code == "invalid_value"
    assert capacities(service_engine) == []
    assert suggestions(service_engine)[0].status == "pending"


def test_a_suggestion_accepts_once(service_engine, user_id, courses):
    submit(service_engine, user_id, *_twenty_class_hours())
    suggestion_id = _pending(service_engine, user_id)
    accept_capacity_suggestion(service_engine, user_id, suggestion_id, "2026-11-02")

    with pytest.raises(StudyError) as excinfo:
        accept_capacity_suggestion(service_engine, user_id, suggestion_id, "2026-11-09")

    assert excinfo.value.code == "already_resolved"
    assert len(capacities(service_engine)) == 1


def test_another_students_suggestion_is_not_found(service_engine, user_id, other_user_id, courses):
    submit(service_engine, user_id, *_twenty_class_hours())

    with pytest.raises(StudyError) as excinfo:
        accept_capacity_suggestion(
            service_engine, other_user_id, _pending(service_engine, user_id), "2026-11-02"
        )

    assert excinfo.value.code == "not_found"
    assert capacities(service_engine) == []


# --- the two front doors (D-216) ---------------------------------------------------------------


def test_the_submit_reply_names_the_suggestion_it_left_pending(service_engine, user_id, courses):
    _, reply = submit(service_engine, user_id, *_twenty_class_hours())

    assert reply["capacity_suggestion_id"] == _pending(service_engine, user_id)


def test_the_dashboard_accepts_through_the_same_service(service_engine, user_id, courses):
    from starlette.testclient import TestClient

    from studysystem.dashboard import create_app

    submit(service_engine, user_id, *_twenty_class_hours())
    client = TestClient(create_app(service_engine), base_url="http://127.0.0.1:8765")

    response = client.post(
        "/capacity/accept",
        json={"suggestion_id": _pending(service_engine, user_id), "from_date": "2026-11-02"},
    )

    assert response.status_code == 200, response.text
    assert response.json()["hours_per_week"] == 10.0
    assert [r.from_date for r in capacities(service_engine)] == ["2026-11-02"]
    assert suggestions(service_engine)[0].status == "accepted"


# --- dropping a course (D-217) ------------------------------------------------------------------


def test_dropping_a_course_recounts_the_pending_suggestion(service_engine, user_id, courses):
    """A course he does not take leaves the suggestion at once (D-217): 4 hours of I3302 and 2
    of I3303 suggest 3; I3303 dropped, 2 remain."""
    submit(
        service_engine,
        user_id,
        meeting("I3302", 1, "08:00", "10:00"),
        meeting("I3302", 3, "08:00", "10:00"),
        meeting("I3303", 5, "10:00", "12:00"),
    )

    result = drop_course(service_engine, user_id, "I3303")

    assert result["dropped_at"] is not None
    assert result["suggestion"]["hours_per_week"] == 2.0
    assert sorted((r.hours_per_week, r.status) for r in suggestions(service_engine)) == [
        (2.0, "pending"),
        (3.0, "dismissed"),
    ]
    assert len(meetings_in(service_engine)) == 3  # the rows stay, as history


def test_a_dropped_course_is_not_offered_and_its_code_is_refused(service_engine, user_id, courses):
    drop_course(service_engine, user_id, "I3305")

    task = start_generation(service_engine, user_id, "timetable")
    assert "I3305" not in {c["code"] for c in task.payload["rules"]["courses"]}

    reply = submit_generation(
        service_engine,
        user_id,
        task.payload["task_id"],
        {"meetings": [meeting("I3305", 3, "14:00", "16:00")]},
    )
    assert [(e["code"], e["field"]) for e in reply["item_errors"]] == [
        ("unknown_course", "course_code")
    ]
    assert meetings_in(service_engine) == []


def test_a_dropped_course_leaves_the_state_and_its_meetings_leave_the_week(
    service_engine, user_id, courses
):
    import datetime

    from studysystem.services.state import get_state

    submit(
        service_engine,
        user_id,
        meeting("I3302", 1, "08:00", "10:00"),
        meeting("I3303", 5, "10:00", "12:00"),
    )
    drop_course(service_engine, user_id, "I3303")

    sent = get_state(service_engine, user_id, datetime.date(2026, 11, 2), "test")
    assert "I3303" not in {c["code"] for c in sent["courses"]}
    assert {m["code"] for m in sent["timetable"]["meetings"]} == {"I3302"}
    assert "I3303" not in {a["code"] for a in sent["assessment_slots"]}


def test_a_course_drops_once(service_engine, user_id, courses):
    drop_course(service_engine, user_id, "I3303")

    with pytest.raises(StudyError) as excinfo:
        drop_course(service_engine, user_id, "I3303")

    assert excinfo.value.code == "already_dropped"


def test_a_second_timetable_task_counts_the_whole_week(service_engine, user_id, courses):
    """I3351 arrives on its own task after the week was read: the one pending suggestion counts
    every meeting he holds, not only the new task's (D-216). 4 + 2 lecture hours suggest 3."""
    submit(
        service_engine,
        user_id,
        meeting("I3302", 1, "08:00", "10:00"),
        meeting("I3302", 3, "08:00", "10:00"),
    )

    _, reply = submit(service_engine, user_id, meeting("I3305", 4, "08:00", "10:00"))

    assert reply["suggested_hours_per_week"] == 3.0
    assert sorted((r.hours_per_week, r.status) for r in suggestions(service_engine)) == [
        (2.0, "dismissed"),
        (3.0, "pending"),
    ]


def test_the_same_number_keeps_the_pending_row(service_engine, user_id, courses):
    """A lab adds no hours: the recount finds the same number and keeps the pending row - no
    dismissed row, the same id in the reply (D-216)."""
    _, first = submit(service_engine, user_id, meeting("I3302", 1, "08:00", "10:00"))

    _, second = submit(service_engine, user_id, meeting("I3305", 3, "14:00", "16:00", "lab"))

    assert second["capacity_suggestion_id"] == first["capacity_suggestion_id"]
    assert [(r.hours_per_week, r.status) for r in suggestions(service_engine)] == [(1.0, "pending")]
