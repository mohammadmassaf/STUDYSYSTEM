"""The shortfall detector (4.9; D-292 - D-300): the gather, the three pure pieces, the ladder
(`shortfall_of`) and his answer (`respond_to_offer`). Numbers are his live data on 2026-11-15.
"""

import datetime
import json

import pytest
from sqlalchemy import insert, select

from studysystem.db.tables import course, hours_entry
from studysystem.errors import StudyError
from studysystem.services import state
from studysystem.services.assessments import add_assessment, set_assessment_input
from studysystem.services.capacity import set_capacity
from studysystem.services.courses import add_course, drop_course, set_course_input
from studysystem.services.ids import new_id, now
from studysystem.services.plan import get_plan
from studysystem.services.shortfall import (
    _gather,
    behind_by,
    capacity_until,
    course_need,
    respond_to_offer,
    shortfall_of,
)
from studysystem.services.units import write_unit

SEM = "Fall 2026-2027"
TODAY = datetime.date(2026, 11, 15)


def _hour(engine, user_id, course_id, minutes, occurred_at, voided=False):
    with write_unit(engine) as conn:
        conn.execute(
            insert(hours_entry).values(
                id=new_id(),
                user_id=user_id,
                course_id=course_id,
                minutes=minutes,
                occurred_at=occurred_at,
                source="tool",
                voided_at="2026-11-14T21:00:00Z" if voided else None,
                created_at=now(),
            )
        )


def test_the_gather_reads_courses_in_play_minutes_capacity_and_the_last_exam(
    service_engine, user_id, other_user_id
):
    """One moment, read as plain values: the planned courses still taken with their inputs and
    minutes (all, and the last 14 days), every capacity row oldest first, the latest exam ahead.
    A dropped course, an unplanned one, a voided hour and another user's rows stay out."""
    e, u = service_engine, user_id
    i3302 = add_course(e, u, "I3302", "Web", SEM)["course_id"]
    dhr = add_course(e, u, "DHR300", "Human rights", SEM)["course_id"]
    i3350 = add_course(e, u, "I3350", "Dropped", SEM)["course_id"]
    add_course(e, u, "ENG", "English", SEM, planned=False)
    set_course_input(e, u, "I3302", "credits", 4)
    set_course_input(e, u, "I3302", "target_grade", 75)
    set_course_input(e, u, "DHR300", "credits", 3)
    set_course_input(e, u, "DHR300", "hours_per_credit", 2.7)
    set_course_input(e, u, "DHR300", "elective", True)
    drop_course(e, u, "I3350")

    set_assessment_input(e, u, "I3302", "Final exam", "date", "2027-01-18")
    add_assessment(e, u, "I3302", "Partial exam", "exam", 30)
    set_assessment_input(e, u, "I3302", "Partial exam", "date", "2026-12-07")
    set_assessment_input(e, u, "I3350", "Final exam", "date", "2027-02-01")  # dropped: ignored
    set_assessment_input(e, u, "DHR300", "Final exam", "date", "2026-11-01")  # passed: ignored

    set_capacity(e, u, 14.17, "2026-11-02")
    set_capacity(e, u, 10, "2026-09-27")
    set_capacity(e, other_user_id, 40, "2026-11-01")

    _hour(e, u, i3302, 50, "2026-10-20T18:00:00Z")  # older than 14 days: total only
    _hour(e, u, i3302, 70, "2026-11-02T18:00:00Z")  # the first of the 14 days
    _hour(e, u, i3302, 30, "2026-11-12T18:00:00Z", voided=True)
    _hour(e, u, i3350, 60, "2026-11-10T18:00:00Z")  # a dropped course

    with e.connect() as conn:
        got = _gather(conn, u, TODAY)

    assert [c["code"] for c in got["courses"]] == ["DHR300", "I3302"]
    dhr_row, web = got["courses"]
    assert {k: web[k] for k in ("course_id", "credits", "target_grade", "minutes")} == {
        "course_id": i3302,
        "credits": 4,
        "target_grade": 75,
        "minutes": 120,
    }
    assert web["recent_minutes"] == 70
    assert (web["hours_per_credit"], web["hours_per_credit_tier"]) == (None, "unknown")
    assert (web["strategy"], web["conceded"], web["elective"]) == ("default", False, False)
    assert dhr_row["course_id"] == dhr
    assert (dhr_row["hours_per_credit"], dhr_row["hours_per_credit_tier"]) == (2.7, "declared")
    assert dhr_row["elective"] is True
    assert (dhr_row["minutes"], dhr_row["recent_minutes"]) == (0, 0)
    assert dhr_row["target_grade"] is None

    assert got["capacity"] == [("2026-09-27", 10), ("2026-11-02", 14.17)]
    assert got["last_exam"] == datetime.date(2027, 1, 18)


def test_with_no_exam_ahead_the_last_exam_is_none(service_engine, user_id):
    add_course(service_engine, user_id, "I3302", "Web", SEM)

    with service_engine.connect() as conn:
        got = _gather(conn, user_id, TODAY)

    assert got["last_exam"] is None
    assert got["capacity"] == []


# --- course_need (D-292, D-299) ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("args", "expected"),
    [
        ((4, 6, 75, False, 70), 18 - 70 / 60),  # I3302: 16.83
        ((5, 6, 75, False, 0), 22.5),  # I3351 once its target is set
        ((4, 6, 75, True, 235), 12 - 235 / 60),  # I3304 conceded: read at 50
        ((3, 2.7, 75, False, 0), 6.075),  # DHR300 at its own H
        ((3, 6, 75, False, 1000), 0.0),  # logged past its need: 0, never below
        ((3, 6, 40, True, 0), 7.2),  # conceded under the pass mark: stays at 40
        ((3, 6, None, True, 0), 9.0),  # conceded with no target: the pass mark is known
    ],
)
def test_course_need(args, expected):
    assert course_need(*args) == pytest.approx(expected)


@pytest.mark.parametrize("args", [(5, 6, None, False, 0), (None, 6, 75, False, 0)])
def test_a_course_with_no_target_or_no_credits_has_no_need(args):
    """Unknown stays unknown: the course is left out and named, never counted at a guess."""
    assert course_need(*args) is None


# --- capacity_until (D-293) -------------------------------------------------------------------

ROWS = [("2026-09-27", 10), ("2026-11-02", 14.17), ("2026-12-24", 21), ("2027-01-04", 10)]
FINAL = datetime.date(2027, 1, 18)


@pytest.mark.parametrize(
    ("rows", "today", "last_exam", "expected"),
    [
        (ROWS, TODAY, FINAL, 39 * 14.17 / 7 + 11 * 21 / 7 + 14 * 10 / 7),  # 131.95
        (ROWS, TODAY, datetime.date(2026, 12, 7), 22 * 14.17 / 7),  # to the Partials: 44.53
        (ROWS + [("2026-11-15", 8)], TODAY, FINAL, 39 * 8 / 7 + 33 + 20),  # the copy: 97.57
        (ROWS, FINAL, FINAL, 0.0),  # today is the exam day
        ([("2026-11-20", 7)], TODAY, datetime.date(2026, 11, 22), 2.0),  # before any row: 0
        ([("2026-11-15", 7), ("2026-11-15", 14)], TODAY, datetime.date(2026, 11, 16), 2.0),
    ],
    ids=["his-rows", "to-the-partials", "the-copy", "exam-day", "before-any-row", "tie-later"],
)
def test_capacity_until(rows, today, last_exam, expected):
    assert capacity_until(rows, today, last_exam) == pytest.approx(expected)


# --- behind_by (D-295, D-298) -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("need", "days_left", "recent", "expected"),
    [
        (22.5, 64, 0, 22.5 / 64 * 14),  # I3351: 4.92
        (14.083, 64, 100, 14.083 / 64 * 14 - 100 / 60),  # I3304: 1.41
        (0.0, 64, 30, -0.5),  # target reached: never behind
        (7.0, 0, 0, 98.0),  # the exam is today: days_left counts as 1
    ],
)
def test_behind_by(need, days_left, recent, expected):
    assert behind_by(need, days_left, recent) == pytest.approx(expected)


# --- shortfall_of: the ladder (his piece) -----------------------------------------------------


def _course(code, credits, target, minutes, recent, **more):
    return {
        "course_id": code.ljust(26, "0"),
        "code": code,
        "credits": credits,
        "target_grade": target,
        "conceded": False,
        "strategy": "default",
        "hours_per_credit": None,
        "hours_per_credit_tier": "unknown",
        "elective": False,
        "minutes": minutes,
        "recent_minutes": recent,
    } | more


DHR = dict(hours_per_credit=2.7, hours_per_credit_tier="declared", elective=True)


def his_data(i3351_target=None, capacity=ROWS):
    """`_gather`'s dict on 2026-11-15: his eight courses, H 6 by default, DHR300 at its own 2.7
    and elective, I3351 at 5 credits."""
    return {
        "courses": [
            _course("DHR300", 3, 75, 0, 0, **DHR),
            _course("I3301", 4, 75, 40, 40),
            _course("I3302", 4, 75, 70, 70),
            _course("I3303", 4, 75, 166, 166),
            _course("I3304", 4, 75, 235, 100),
            _course("I3305", 3, 75, 0, 0),
            _course("I3306", 3, 75, 200, 0),
            _course("I3351", 5, i3351_target, 0, 0),
        ],
        "capacity": capacity,
        "last_exam": FINAL,
    }


COPY = ROWS + [("2026-11-15", 8)]  # the Done-when copy: 8 h a week from today


def test_with_spare_hours_only_catch_up_is_offered_to_the_furthest_behind():
    got = shortfall_of(his_data(), TODAY)

    assert got["need"] == pytest.approx(93.22, abs=0.01)
    assert got["capacity"] == pytest.approx(131.95, abs=0.01)
    assert got["gap"] == pytest.approx(38.73, abs=0.01)
    assert got["short"] is False
    assert (got["need_tier"], got["capacity_tier"]) == ("inferred", "declared")
    assert [o["kind"] for o in got["offers"]] == ["catch-up"]
    offer = got["offers"][0]
    assert (offer["code"], offer["tool"]) == ("I3301", "study_respond_to_offer")
    assert offer["hours"] == pytest.approx(3.125, abs=0.01)


def test_a_course_with_no_target_is_named_and_not_counted():
    got = shortfall_of(his_data(), TODAY)

    assert [n["code"] for n in got["not_counted"]] == ["I3351"]
    assert "target" in got["not_counted"][0]["reason"]
    assert "I3351" not in [c["code"] for c in got["courses"]]


def test_the_done_when_copy_is_short_and_offers_catch_up_then_the_three_levers():
    got = shortfall_of(his_data(i3351_target=75, capacity=COPY), TODAY)

    assert got["need"] == pytest.approx(115.72, abs=0.01)
    assert got["capacity"] == pytest.approx(97.57, abs=0.01)
    assert got["gap"] == pytest.approx(-18.15, abs=0.01)
    assert got["short"] is True
    assert got["not_counted"] == []
    kinds = [(o["kind"], o["tool"]) for o in got["offers"]]
    assert kinds == [
        ("catch-up", "study_respond_to_offer"),
        ("lower-target", "study_set_course_input"),
        ("add-capacity", "study_set_capacity"),
        ("concede", "study_respond_to_offer"),
    ]
    assert got["offers"][0]["code"] == "I3351"
    assert got["offers"][0]["hours"] == pytest.approx(4.92, abs=0.01)
    assert all(o["hours"] == pytest.approx(18.15, abs=0.01) for o in got["offers"][1:])


def test_the_list_is_credits_descending_then_code_with_electives_last():
    got = shortfall_of(his_data(i3351_target=75, capacity=COPY), TODAY)

    codes = [c["code"] for c in got["courses"]]
    assert codes == ["I3351", "I3301", "I3302", "I3303", "I3304", "I3305", "I3306", "DHR300"]


def test_each_course_says_its_h_and_the_hours_each_lever_frees():
    got = {c["code"]: c for c in shortfall_of(his_data(i3351_target=75), TODAY)["courses"]}

    assert (got["DHR300"]["h"], got["DHR300"]["h_tier"]) == (2.7, "declared")
    assert (got["I3301"]["h"], got["I3301"]["h_tier"]) == (6.0, "inferred")
    assert got["DHR300"]["frees_conceding"] == pytest.approx(2.03, abs=0.01)
    assert got["I3351"]["frees_conceding"] == pytest.approx(7.5)
    assert got["I3351"]["frees_lowering"] == pytest.approx(3.0)
    assert got["I3305"]["frees_lowering"] == pytest.approx(1.8)
    assert got["I3302"]["need"] == pytest.approx(16.83, abs=0.01)
    assert got["I3304"]["behind"] == pytest.approx(1.41, abs=0.01)


def test_a_course_with_no_credits_is_named_for_its_credits_even_when_conceded():
    data = his_data(i3351_target=None)
    data["courses"][-1] |= {"credits": None, "conceded": True}  # I3351: reads at 50, no credits
    got = shortfall_of(data, TODAY)

    assert got["not_counted"] == [{"code": "I3351", "reason": "no credits"}]


def test_a_conceded_course_reads_at_the_pass_mark_and_frees_nothing_more():
    data = his_data(i3351_target=75)
    data["courses"][-1]["conceded"] = True  # I3351
    got = {c["code"]: c for c in shortfall_of(data, TODAY)["courses"]}

    assert got["I3351"]["need"] == pytest.approx(15.0)  # 5 x 6 x 50 / 100
    assert got["I3351"]["conceded"] is True
    assert (got["I3351"]["frees_conceding"], got["I3351"]["frees_lowering"]) == (0, 0)


def test_catch_up_skips_a_course_already_on_it_and_goes_to_the_next():
    data = his_data()
    data["courses"][1]["strategy"] = "catch-up"  # I3301, the furthest behind
    got = shortfall_of(data, TODAY)

    assert got["offers"][0]["code"] == "I3305"  # 2.95 h behind, the next furthest
    assert got["offers"][0]["hours"] == pytest.approx(2.95, abs=0.01)


def test_nobody_behind_means_no_catch_up_offer():
    data = his_data()
    for c in data["courses"]:
        c["recent_minutes"] = 600  # 10 h each in the last 14 days: everyone ahead
    got = shortfall_of(data, TODAY)

    assert got["offers"] == []


def test_with_no_exam_ahead_there_is_no_window_and_no_offer():
    data = his_data() | {"last_exam": None}
    got = shortfall_of(data, TODAY)

    assert got["need"] == pytest.approx(93.22, abs=0.01)
    assert (got["capacity"], got["gap"], got["short"]) == (None, None, None)
    assert got["capacity_tier"] == "unknown"
    assert got["offers"] == []


# --- respond_to_offer (D-296, D-299) ----------------------------------------------------------


@pytest.fixture
def two_courses(service_engine, user_id):
    add_course(service_engine, user_id, "I3351", "Networks lab", SEM)
    add_course(service_engine, user_id, "DHR300", "Human rights", SEM)


def _row(engine, code):
    with engine.connect() as conn:
        return conn.execute(select(course).where(course.c.code == code)).one()


def test_catch_up_saves_the_strategy_and_its_date(service_engine, user_id, two_courses):
    got = respond_to_offer(service_engine, user_id, "i3351", "catch-up")

    assert (got["code"], got["strategy"], got["conceded"]) == ("I3351", "catch-up", False)
    assert got["strategy_changed_at"] is not None
    row = _row(service_engine, "I3351")
    assert (row.strategy, row.strategy_changed_at) == ("catch-up", got["strategy_changed_at"])
    assert _row(service_engine, "DHR300").strategy == "default"  # one course only


def test_concede_then_unconcede_sets_and_clears_the_date(service_engine, user_id, two_courses):
    got = respond_to_offer(service_engine, user_id, "DHR300", "concede")
    assert got["conceded"] is True
    assert got["conceded_at"] is not None

    got = respond_to_offer(service_engine, user_id, "DHR300", "unconcede")
    assert (got["conceded"], got["conceded_at"]) == (False, None)
    row = _row(service_engine, "DHR300")
    assert (row.conceded, row.conceded_at) == (0, None)


@pytest.mark.parametrize(
    ("first", "again"),
    [("catch-up", "catch-up"), (None, "default"), (None, "unconcede"), ("concede", "concede")],
)
def test_the_state_a_course_is_already_in_is_already_set(
    service_engine, user_id, two_courses, first, again
):
    if first is not None:
        respond_to_offer(service_engine, user_id, "I3351", first)
    before = _row(service_engine, "I3351")

    with pytest.raises(StudyError) as excinfo:
        respond_to_offer(service_engine, user_id, "I3351", again)

    assert excinfo.value.code == "already_set"
    assert _row(service_engine, "I3351") == before


def test_an_unknown_choice_is_invalid_and_names_the_four(service_engine, user_id, two_courses):
    with pytest.raises(StudyError) as excinfo:
        respond_to_offer(service_engine, user_id, "I3351", "give-up")

    assert excinfo.value.code == "invalid_value"
    assert all(c in excinfo.value.fix for c in ("catch-up", "default", "concede", "unconcede"))


def test_a_dropped_course_gets_no_answer(service_engine, user_id, two_courses):
    drop_course(service_engine, user_id, "I3351")

    with pytest.raises(StudyError) as excinfo:
        respond_to_offer(service_engine, user_id, "I3351", "catch-up")

    assert excinfo.value.code == "not_in_plan"
    assert _row(service_engine, "I3351").strategy == "default"


def test_another_users_course_is_not_found(service_engine, user_id, other_user_id, two_courses):
    with pytest.raises(StudyError) as excinfo:
        respond_to_offer(service_engine, other_user_id, "I3351", "catch-up")

    assert excinfo.value.code == "not_found"


# --- the wiring: the tool, GET /state, the plan reply -------------------------------------------


def _body(result) -> dict:
    return json.loads(result.content[0].text)


def test_the_tool_records_the_answer_and_errors_come_back_structured(call, two_courses):
    ok = call("study_respond_to_offer", {"code": "DHR300", "choice": "concede"})
    assert not ok.is_error
    assert _body(ok)["conceded"] is True

    again = call("study_respond_to_offer", {"code": "DHR300", "choice": "concede"})
    assert again.is_error
    assert _body(again)["code"] == "already_set"


def _short_semester(engine, user_id, today):
    """Two courses with targets, a Final 30 days out and 2 h a week: short."""
    for code in ("I3301", "I3302"):
        add_course(engine, user_id, code, code, SEM)
        set_course_input(engine, user_id, code, "credits", 4)
        set_course_input(engine, user_id, code, "target_grade", 75)
        final = (today + datetime.timedelta(days=30)).isoformat()
        set_assessment_input(engine, user_id, code, "Final exam", "date", final)
    set_capacity(engine, user_id, 2, (today - datetime.timedelta(days=1)).isoformat())


def test_state_sends_the_alert_with_every_uncertain_figure_tiered(service_engine, user_id):
    today = datetime.date(2026, 11, 18)
    _short_semester(service_engine, user_id, today)

    sent = state.get_state(service_engine, user_id, today, "0.1.0")["shortfall"]

    assert (sent["need"], sent["need_tier"]) == (36.0, "inferred")  # 2 x 4 x 6 x 0.75
    assert (sent["capacity"], sent["capacity_tier"]) == (pytest.approx(8.57, abs=0.01), "declared")
    assert sent["short"] is True
    assert [o["kind"] for o in sent["offers"]] == [
        "catch-up",
        "lower-target",
        "add-capacity",
        "concede",
    ]
    first = sent["courses"][0]
    assert (first["code"], first["h"], first["h_tier"]) == ("I3301", 6.0, "inferred")
    assert first["behind_tier"] == "inferred"


def test_state_with_no_exam_ahead_sends_capacity_gap_and_short_unknown(service_engine, user_id):
    add_course(service_engine, user_id, "I3301", "I3301", SEM)

    sent = state.get_state(service_engine, user_id, TODAY, "0.1.0")["shortfall"]

    assert (sent["capacity"], sent["capacity_tier"]) == (None, "unknown")
    assert (sent["gap"], sent["short"], sent["short_tier"]) == (None, None, "unknown")
    assert sent["not_counted"] == [{"code": "I3301", "reason": "no credits and no target grade"}]


def test_the_plan_reply_carries_the_alert_and_stores_nothing_of_it(service_engine, user_id):
    today = datetime.date(2026, 11, 18)
    _short_semester(service_engine, user_id, today)

    reply = get_plan(service_engine, user_id, today)

    assert reply["shortfall"]["short"] is True
    assert reply["shortfall"]["gap"] == pytest.approx(8.57 - 36, abs=0.01)
    assert reply["shortfall"]["offers"][0]["code"] == "I3301"
