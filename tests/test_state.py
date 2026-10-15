"""`GET /state` (3.2; D-19, D-22, D-163): one builder per section, each checked against its own
schema, every uncertain value with its tier - on his I3302 and a cold I3350."""

import datetime
import importlib.metadata

import pytest
from jsonschema import Draft202012Validator, ValidationError
from sqlalchemy import func, insert, select
from starlette.testclient import TestClient

from studysystem.dashboard import create_app
from studysystem.db.tables import class_meeting, plan
from studysystem.services import state
from studysystem.services.capacity import set_capacity
from studysystem.services.courses import add_course
from studysystem.services.ids import new_id, now
from studysystem.services.plan import get_plan
from studysystem.services.units import write_unit
from tests.test_plan import SEM, TODAY, _course, _i3302_and_a_cold_course

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
    }


def _meeting(conn, uid, course_id, weekday, tier, room=None):
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
