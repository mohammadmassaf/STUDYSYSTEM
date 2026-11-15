"""`GET /state` (3.2; D-19, D-22, D-163): one builder per section, each checked against its own
schema, every uncertain value with its tier - on his I3302 and a cold I3350."""

import datetime
import hashlib
import importlib.metadata

import pytest
from jsonschema import Draft202012Validator, ValidationError
from sqlalchemy import func, insert, select, update
from starlette.testclient import TestClient

from studysystem.dashboard import create_app
from studysystem.db.tables import (
    assessment_slot,
    class_meeting,
    material,
    past_exam,
    plan,
    plan_item,
)
from studysystem.services import state
from studysystem.services.capacity import set_capacity
from studysystem.services.courses import add_course, drop_course, set_course_input
from studysystem.services.ids import new_id, now
from studysystem.services.plan import get_plan
from studysystem.services.units import read_unit, write_unit
from tests.test_adherence import _hours
from tests.test_plan import SEM, TODAY, _course, _i3302_and_a_cold_course, _i3302_taught

OWN = "http://127.0.0.1:8765"


def blob(engine, user_id, today=TODAY) -> dict:
    return state.get_state(engine, user_id, today, "0.1.0")


def plans(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(plan)).scalar_one()


def test_an_empty_database_is_unknown_everywhere(service_engine, user_id):
    sent = blob(service_engine, user_id)

    assert sent["semester"] == {
        "name": None,
        "name_tier": "unknown",
        "start": None,
        "start_tier": "unknown",
        "end": None,
        "end_tier": "unknown",
        "end_approx": False,
    }
    assert sent["capacity"]["declared_hours_per_week"] is None
    assert sent["capacity"]["declared_hours_per_week_tier"] == "unknown"
    assert sent["timetable"] == {"meetings": None, "meetings_tier": "unknown"}
    assert sent["plan"] == {"current": None, "current_tier": "unknown"}
    assert sent["assessment_slots"] == [] and sent["courses"] == []


def test_a_new_course_sends_unknown_not_zero(service_engine, user_id):
    add_course(service_engine, user_id, "I3302", "Server-Side Web Development", SEM)

    [web] = blob(service_engine, user_id)["courses"]

    assert (web["credits"], web["credits_tier"]) == (None, "unknown")
    assert (web["instructor"], web["instructor_tier"]) == (None, "unknown")
    assert (web["target_grade"], web["target_grade_tier"]) == (None, "unknown")
    assert web["minutes_logged"] == 0  # hours are explicit: none logged is a real 0
    assert blob(service_engine, user_id)["semester"]["name"] == SEM


def test_the_plan_is_the_latest_stored_one_and_none_is_written(service_engine, user_id):
    with write_unit(service_engine) as conn:
        ids = _i3302_and_a_cold_course(conn, user_id)
    get_plan(service_engine, user_id, TODAY, shown_n=5)
    latest = get_plan(service_engine, user_id, TODAY, shown_n=2)

    sent = blob(service_engine, user_id)["plan"]

    assert plans(service_engine) == 2  # reading made no plan (D-163)
    current = sent["current"]
    assert sent["current_tier"] == "inferred"
    assert current["plan_id"] == latest["plan_id"] and current["shown_n"] == 2
    study = current["lanes"]["study"]
    assert [item["topic_id"] for item in study["items"]] == [ids["MySQL"], ids["sessions"]]
    assert study["hidden"] == 1
    assert study["items"][0]["reason"] == latest["lanes"]["study"]["items"][0]["reason"]
    assert (
        study["items"][0]["estimated_minutes"],
        study["items"][0]["estimated_minutes_tier"],
    ) == (
        60,
        "inferred",
    )
    setup = current["lanes"]["setup"]["items"]
    assert [item["setup_kind"] for item in setup] == ["first-material", "past-papers"]
    assert all(item["marks_unlocked_estimate_tier"] == "inferred" for item in setup)
    assert set(current["strategy_by_course"].values()) == {"default"}


def test_the_study_lane_comes_grouped_by_chapter(service_engine, user_id):
    # D-238: the page lists what the server grouped. Both front doors read one grouping.
    with write_unit(service_engine) as conn:
        _i3302_taught(conn, user_id)
    made = get_plan(service_engine, user_id, TODAY, shown_n=2)["lanes"]["study"]["entries"]

    study = blob(service_engine, user_id)["plan"]["current"]["lanes"]["study"]

    with service_engine.connect() as conn:
        filename = conn.execute(select(material.c.filename)).scalar_one()
    [entry] = study["entries"]
    assert entry["chapter"] == filename.removesuffix(".pdf")
    assert (entry["rank"], entry["topics"]) == (1, ["MySQL", "sessions"])
    assert (entry["estimated_minutes"], entry["estimated_minutes_tier"]) == (120, "inferred")
    assert entry["material_id"] == made[0]["material_id"]
    assert (study["items"][0]["material_id"], study["items"][0]["material_id_tier"]) == (
        made[0]["material_id"],
        "inferred",
    )


def test_a_plan_stored_before_chapters_lists_its_topics_alone(service_engine, user_id):
    # D-240: its items stay NULL, so each topic is its own entry until a new plan. A topic with
    # no chapter in a new plan has the same shape (D-244).
    with write_unit(service_engine) as conn:
        ids = _i3302_taught(conn, user_id)
    get_plan(service_engine, user_id, TODAY, shown_n=2)
    with write_unit(service_engine) as conn:
        conn.execute(update(plan_item).values(material_id=None))

    sent = blob(service_engine, user_id)["plan"]

    state.CHECKS["plan"].validate(sent)
    study = sent["current"]["lanes"]["study"]
    assert [(e["rank"], e["topic_ids"]) for e in study["entries"]] == [
        (1, [ids["MySQL"]]),
        (2, [ids["sessions"]]),
    ]
    assert (study["entries"][0]["material_id"], study["entries"][0]["material_id_tier"]) == (
        None,
        "unknown",
    )
    assert (study["items"][0]["chapter"], study["items"][0]["chapter_tier"]) == (None, "unknown")


def test_each_assessment_carries_its_newest_profile_or_unknown(service_engine, user_id):
    with write_unit(service_engine) as conn:
        _i3302_and_a_cold_course(conn, user_id)

    slots = blob(service_engine, user_id)["assessment_slots"]

    # Soonest first: I3302's Partial (Nov 16), then both Finals (Jan 18) by code.
    assert [(s["code"], s["name"]) for s in slots] == [
        ("I3302", "Partial exam"),
        ("I3302", "Final exam"),
        ("I3350", "Final exam"),
    ]
    partial, final, cold = slots
    assert (final["profile_tier"], final["profile"]["version"]) == ("inferred", 1)
    assert (partial["profile"], partial["profile_tier"]) == (None, "unknown")
    assert (final["weight"], final["weight_tier"]) == (70.0, "declared")
    assert (final["room"], final["room_tier"]) == (None, "unknown")
    assert cold["date_approx"] is True


def test_capacity_is_the_row_in_force_today(service_engine, user_id):
    set_capacity(service_engine, user_id, 10, "2026-09-27")
    set_capacity(service_engine, user_id, 21, "2026-12-24")  # the winter break, later

    sent = blob(service_engine, user_id)["capacity"]

    assert sent == {
        "declared_hours_per_week": 10.0,
        "declared_hours_per_week_tier": "declared",
        "from_date": "2026-09-27",
        "from_date_tier": "declared",
        "suggested_from_timetable": None,  # no timetable read yet
        "suggested_from_timetable_tier": "unknown",
        "suggestion_id": None,
        "suggestion_id_tier": "unknown",
    }


def _meeting(conn, uid, course_id, weekday, tier, room=None, group=None):
    conn.execute(
        insert(class_meeting).values(
            id=new_id(),
            user_id=uid,
            course_id=course_id,
            weekday=weekday,
            start_time="09:00",
            end_time="11:00",
            room=room,
            kind="lecture",
            tier=tier,
            group_label=group,
            created_at=now(),
        )
    )


def test_a_week_with_one_unconfirmed_meeting_is_inferred(service_engine, user_id):
    with write_unit(service_engine) as conn:
        web = _course(conn, user_id)
        _meeting(conn, user_id, web, 1, "declared")
        _meeting(conn, user_id, web, 3, "inferred", room="B204")  # read off his timetable image

    sent = blob(service_engine, user_id)["timetable"]

    assert sent["meetings_tier"] == "inferred"
    monday, wednesday = sent["meetings"]
    assert (monday["weekday"], monday["room"], monday["room_tier"]) == (1, None, "unknown")
    # The room is as unconfirmed as the meeting it was read with.
    assert (wednesday["room"], wednesday["room_tier"]) == ("B204", "inferred")


def test_a_course_with_a_lab_group_sends_only_that_groups_meetings(service_engine, user_id):
    """D-227: he is in G1 for I3302 - its whole-class meeting and the G1 meeting come, the G2
    one does not. I3305 has no group set, so every group still comes. Set as "g1": case is not part
    of the name."""
    with write_unit(service_engine) as conn:
        web = _course(conn, user_id)
        _meeting(conn, user_id, web, 1, "declared")
        _meeting(conn, user_id, web, 2, "declared", group="G1")
        _meeting(conn, user_id, web, 3, "declared", group="G2")
        gui = _course(conn, user_id, "I3305")
        _meeting(conn, user_id, gui, 4, "declared", group="G2")
    set_course_input(service_engine, user_id, "I3302", "lab_group", "g1")

    sent = blob(service_engine, user_id)["timetable"]["meetings"]

    assert [(m["code"], m["weekday"], m["group_label"]) for m in sent] == [
        ("I3302", 1, ""),
        ("I3302", 2, "G1"),
        ("I3305", 4, "G2"),
    ]


def test_another_students_rows_never_show(service_engine, user_id, other_user_id):
    with write_unit(service_engine) as conn:
        _i3302_and_a_cold_course(conn, other_user_id)
        _meeting(conn, other_user_id, _course(conn, other_user_id, "I3301"), 1, "declared")
    get_plan(service_engine, other_user_id, TODAY)
    set_capacity(service_engine, other_user_id, 10, "2026-09-27")

    sent = blob(service_engine, user_id)

    assert sent["courses"] == [] and sent["assessment_slots"] == []
    assert sent["plan"]["current"] is None
    assert sent["timetable"]["meetings"] is None
    assert sent["capacity"]["declared_hours_per_week"] is None
    assert sent["semester"]["name"] is None


@pytest.mark.parametrize(
    "capacity",
    [
        {"declared_hours_per_week": None, "declared_hours_per_week_tier": "declared"},
        {"declared_hours_per_week": 0, "declared_hours_per_week_tier": "unknown"},  # 0 for unknown
    ],
)
def test_the_schema_refuses_a_pair_that_breaks_the_tier_rule(capacity):
    section = {
        **capacity,
        "from_date": None,
        "from_date_tier": "unknown",
        "suggested_from_timetable": None,
        "suggested_from_timetable_tier": "unknown",
        "suggestion_id": None,
        "suggestion_id_tier": "unknown",
    }

    with pytest.raises(ValidationError):
        state.CHECKS["capacity"].validate(section)


def _nullable_without_tier(schema, where="#") -> list[str]:
    """Every property that accepts null with no `<name>_tier` beside it."""
    if not isinstance(schema, dict):
        return []
    found = []
    properties = schema.get("properties", {})
    for name, sub in properties.items():
        accepts_null = Draft202012Validator(sub).is_valid(None)
        if accepts_null and f"{name}_tier" not in properties:
            found.append(f"{where}/{name}")
        found += _nullable_without_tier(sub, f"{where}/{name}")
    for key in ("items", "additionalProperties"):
        found += _nullable_without_tier(schema.get(key), f"{where}/{key}")
    for n, sub in enumerate(schema.get("anyOf", [])):
        found += _nullable_without_tier(sub, f"{where}/anyOf/{n}")
    return found


@pytest.mark.parametrize("name", list(state.SECTIONS))
def test_no_section_can_send_a_bare_null(name):
    # Done when: every uncertain value carries its tier.
    _, schema = state.SECTIONS[name]
    assert _nullable_without_tier(schema) == []


def test_get_state_serves_every_built_section(service_engine, user_id):
    with write_unit(service_engine) as conn:
        _i3302_and_a_cold_course(conn, user_id)
    get_plan(service_engine, user_id, datetime.datetime.now().astimezone().date())
    client = TestClient(create_app(service_engine), base_url=OWN)

    response = client.get("/state")

    assert response.status_code == 200
    body = response.json()
    assert list(body) == ["server_version", "generated_at", *state.SECTIONS]
    assert body["server_version"] == importlib.metadata.version("studysystem")
    for name, check in state.CHECKS.items():
        check.validate(body[name])  # the JSON that left, not the dict before it


def test_get_state_answers_only_our_own_host(service_engine, user_id):
    client = TestClient(create_app(service_engine), base_url="http://evil.example")

    assert client.get("/state").status_code == 403


def test_a_course_dropped_after_the_plan_leaves_the_stored_plan(service_engine, user_id):
    """D-217, read side: the stored plan still ranks I3350, built before he dropped it. /state
    leaves it out; the stored rows stay as they were ranked."""
    with write_unit(service_engine) as conn:
        _i3302_and_a_cold_course(conn, user_id)
    get_plan(service_engine, user_id, TODAY, shown_n=50)
    dropped = drop_course(service_engine, user_id, "I3350")["course_id"]

    current = state.get_state(service_engine, user_id, TODAY, "test")["plan"]["current"]

    codes = {item["code"] for lane in current["lanes"].values() for item in lane["items"]}
    assert codes == {"I3302"}
    assert dropped not in current["strategy_by_course"]
    with service_engine.connect() as conn:
        kept = conn.execute(
            select(func.count()).select_from(plan_item).where(plan_item.c.course_id == dropped)
        ).scalar_one()
    assert kept > 0  # history


# --- guess sensitivity (4.8; D-281, D-282) ------------------------------------------------------


def test_metrics_send_the_latest_plans_guess_count_with_its_shown_count(service_engine, user_id):
    with write_unit(service_engine) as conn:
        _i3302_taught(conn, user_id)
    result = get_plan(service_engine, user_id, TODAY, shown_n=2)
    with write_unit(service_engine) as conn:
        conn.execute(update(plan).where(plan.c.id == result["plan_id"]).values(guess_changed=1))

    sent = blob(service_engine, user_id)["metrics"]["guess_sensitivity"]

    assert sent == {"shown": 2, "changed": 1, "changed_tier": "inferred"}


def test_a_plan_with_no_guess_count_sends_unknown_not_zero(service_engine, user_id):
    # Case 6: a plan stored before 0024, or one that showed no study topic.
    with write_unit(service_engine) as conn:
        _i3302_taught(conn, user_id)
    result = get_plan(service_engine, user_id, TODAY, shown_n=2)
    with write_unit(service_engine) as conn:
        conn.execute(update(plan).where(plan.c.id == result["plan_id"]).values(guess_changed=None))

    sent = blob(service_engine, user_id)["metrics"]["guess_sensitivity"]

    assert sent == {"shown": 2, "changed": None, "changed_tier": "unknown"}


def test_no_plan_yet_sends_no_guess_count(service_engine, user_id):
    sent = blob(service_engine, user_id)["metrics"]["guess_sensitivity"]

    assert sent == {"shown": 0, "changed": None, "changed_tier": "unknown"}


# --- study time: today, the week by day, the streak (D-283, D-286) ------------

MON = datetime.date(2026, 11, 9)
DAY = datetime.timedelta(days=1)


def _noon(day: datetime.date) -> str:
    """Midday UTC: the same day on every clock from UTC-11 to UTC+11, so the test runs anywhere."""
    return f"{day.isoformat()}T12:00:00Z"


def test_study_time_with_no_hours_is_a_real_zero(service_engine, user_id):
    assert blob(service_engine, user_id, MON)["study_time"] == {
        "today_minutes": 0,
        "week_start": "2026-11-09",
        "week_days": [0, 0, 0, 0, 0, 0, 0],
        "streak_days": 0,
        "streak_at_risk": False,
    }


def test_study_time_sums_today_and_each_day_of_the_week(service_engine, user_id):
    with write_unit(service_engine) as conn:
        web = _course(conn, user_id)
    wednesday = MON + 2 * DAY
    _hours(service_engine, user_id, web, _noon(MON - DAY), 30)  # Sunday: the week before
    _hours(service_engine, user_id, web, _noon(MON), 70)
    _hours(service_engine, user_id, web, _noon(wednesday), 40)
    _hours(service_engine, user_id, web, _noon(wednesday), 20)
    _hours(service_engine, user_id, web, _noon(wednesday), 90, voided=True)

    sent = blob(service_engine, user_id, wednesday)["study_time"]

    assert sent["today_minutes"] == 60
    assert sent["week_start"] == "2026-11-09"
    assert sent["week_days"] == [70, 0, 60, 0, 0, 0, 0]
    assert (sent["streak_days"], sent["streak_at_risk"]) == (1, False)  # Tuesday was empty


@pytest.mark.parametrize(
    ("studied", "today", "expected"),
    [
        ([], 13, (0, False)),
        ([9, 10, 11, 12], 13, (4, True)),  # Mon-Thu, Friday with nothing yet: kept, at risk
        ([9, 10, 11, 12, 13], 13, (5, False)),
        ([9, 10, 11, 12], 14, (0, False)),  # Friday ended empty: the streak ended
        ([9, 11, 12], 12, (2, False)),  # the gap on the 10th stops the count
        ([13], 13, (1, False)),
    ],
)
def test_the_streak_ends_when_a_day_ends_empty(studied, today, expected):
    days = {datetime.date(2026, 11, day) for day in studied}

    assert state.streak(days, datetime.date(2026, 11, today)) == expected


def test_an_hour_belongs_to_the_day_on_the_students_clock(service_engine, user_id):
    with write_unit(service_engine) as conn:
        web = _course(conn, user_id)
    _hours(service_engine, user_id, web, "2026-11-08T22:30:00Z", 45)  # Monday 00:30 in Beirut

    with read_unit(service_engine) as conn:
        beirut = datetime.timezone(datetime.timedelta(hours=2))
        days = state.day_minutes(conn, user_id, beirut)

    assert days == {MON: 45}


def test_another_students_hours_never_count(service_engine, user_id, other_user_id):
    with write_unit(service_engine) as conn:
        theirs = _course(conn, other_user_id)
    _hours(service_engine, other_user_id, theirs, _noon(MON), 50)

    sent = blob(service_engine, user_id, MON)["study_time"]

    assert (sent["today_minutes"], sent["streak_days"]) == (0, 0)


# --- past papers on the course card (D-285) -----------------------------------


def _paper(conn, uid, course_id, slot_name, session_date, approx=False) -> str:
    slot_id = conn.execute(
        select(assessment_slot.c.id).where(
            assessment_slot.c.course_id == course_id, assessment_slot.c.name == slot_name
        )
    ).scalar()
    if slot_id is None:
        slot_id = new_id()
        conn.execute(
            insert(assessment_slot).values(
                id=slot_id,
                owner_id=uid,
                course_id=course_id,
                name=slot_name,
                kind="exam",
                created_at=now(),
            )
        )
    paper_id = new_id()
    conn.execute(
        insert(past_exam).values(
            id=paper_id,
            owner_id=uid,
            slot_id=slot_id,
            session_type="first",
            session_date=session_date,
            session_date_approx=int(approx),
            session_delayed=0,
            instructor=None,
            instructor_tier="unknown",
            has_text_layer=1,
            file_ref=f"papers/{paper_id}.pdf",
            content_sha256=hashlib.sha256(paper_id.encode()).hexdigest(),
            created_at=now(),
        )
    )
    return paper_id


def test_a_course_sends_its_past_papers_oldest_first(service_engine, user_id, other_user_id):
    with write_unit(service_engine) as conn:
        web = _course(conn, user_id)
        _course(conn, user_id, code="I3303")
        theirs = _course(conn, other_user_id)
        late = _paper(conn, user_id, web, "Final exam", "2019-01-24")
        early = _paper(conn, user_id, web, "Partial exam", "2018-11-01", approx=True)
        _paper(conn, other_user_id, theirs, "Final exam", "2020-02-17")

    by_code = {sent["code"]: sent for sent in blob(service_engine, user_id)["courses"]}

    assert by_code["I3302"]["past_papers"] == [
        {
            "past_exam_id": early,
            "assessment_name": "Partial exam",
            "session_type": "first",
            "session_date": "2018-11-01",
            "session_date_approx": True,
        },
        {
            "past_exam_id": late,
            "assessment_name": "Final exam",
            "session_type": "first",
            "session_date": "2019-01-24",
            "session_date_approx": False,
        },
    ]
    assert by_code["I3303"]["past_papers"] == []


def test_a_dropped_course_sends_no_papers(service_engine, user_id):
    with write_unit(service_engine) as conn:
        mobile = _course(conn, user_id, code="I3350")
        _paper(conn, user_id, mobile, "Final exam", "2021-01-20")
    drop_course(service_engine, user_id, "I3350")

    assert blob(service_engine, user_id)["courses"] == []  # D-217: its papers stay as history
