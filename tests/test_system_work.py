"""System work on the Plan page (4.12; D-25, D-310, D-311): zero-estimate past papers and proposed
directives, listed only when the plan has room, and a directive confirmed from the page lands as
the same row the tool writes.

The plan rows are written directly - shorter than the scheduler run that would rank them. I3304's
Partial is the zero-estimate item throughout, as in his live plan on 2026-11-19."""

# ruff: noqa: F811 - the fixtures are imported, then named as test parameters

import datetime

import pytest
from sqlalchemy import insert, select
from starlette.testclient import TestClient

from studysystem.dashboard import create_app
from studysystem.db.tables import generation_directive, plan, plan_item
from studysystem.services import state
from studysystem.services.courses import add_course, drop_course
from studysystem.services.feedback import submit_feedback
from studysystem.services.ids import new_id, now
from studysystem.services.system_work import system_work
from studysystem.services.units import write_unit
from tests.test_active_directives import directive

SEM = "Semester 1 2026-2027"
OWN = "http://127.0.0.1:8765"
ROOM = {"gap": 7.24}  # the demo's surplus: 104.43 h capacity, 97.19 h need
SHORT = {"gap": -8.83}  # his live plan on 2026-11-19
ZERO_REASON = (
    "I3304 Partial exam: get more past papers - the profile has its papers; papers 4-5 unlock "
    "only the mock-exam gate (D-24) · moves no marks itself"
)
MCQ = "Prefer short MCQ with four options."
EMPTY = {"papers": [], "directives": []}


def _course(engine, user_id, code) -> str:
    return add_course(engine, user_id, code, f"Course {code}", SEM)["course_id"]


def _plan(engine, user_id, items, scope="all-courses", scope_course_id=None) -> str:
    """One stored plan holding `items`: (course_id, setup_kind, setup_key, estimate, shown,
    reason), ranked in the order given, all in the setup lane."""
    plan_id = new_id()
    with write_unit(engine) as conn:
        conn.execute(
            insert(plan).values(
                id=plan_id,
                user_id=user_id,
                scope=scope,
                scope_course_id=scope_course_id,
                shown_n=5,
                strategy_snapshot="{}",
                created_at=now(),
            )
        )
        for rank, (course_id, kind, key, estimate, shown, reason) in enumerate(items, start=1):
            conn.execute(
                insert(plan_item).values(
                    id=new_id(),
                    user_id=user_id,
                    plan_id=plan_id,
                    lane="setup",
                    course_id=course_id,
                    setup_kind=kind,
                    setup_key=key,
                    rank=rank,
                    shown=int(shown),
                    reason=reason,
                    marks_unlocked_estimate=estimate,
                )
            )
    return plan_id


def work(engine, user_id, alert) -> dict:
    with engine.connect() as conn:
        return system_work(conn, user_id, alert)


@pytest.fixture
def i3304(service_engine, user_id) -> str:
    """I3304 with its zero-estimate Partial item, hidden, in the latest plan."""
    course_id = _course(service_engine, user_id, "I3304")
    _plan(
        service_engine,
        user_id,
        [(course_id, "past-papers", "slot-partial", 0.0, False, ZERO_REASON)],
    )
    return course_id


def PAPER(course_id) -> dict:  # noqa: N802 - reads as the constant it stands for
    return {
        "course_id": course_id,
        "code": "I3304",
        "setup_key": "slot-partial",
        "reason": ZERO_REASON,
    }


# --- the gate (D-310): room means gap above 0 ----------------------------------------------------


def test_short_of_capacity_lists_nothing_though_both_rows_exist(service_engine, user_id, i3304):
    directive(service_engine, user_id, MCQ, status="proposed")

    assert work(service_engine, user_id, SHORT) == EMPTY


def test_a_surplus_lists_the_zero_paper_and_the_proposed_directive(service_engine, user_id, i3304):
    i3302 = _course(service_engine, user_id, "I3302")
    made = directive(service_engine, user_id, MCQ, course_id=i3302, status="proposed")

    assert work(service_engine, user_id, ROOM) == {
        "papers": [PAPER(i3304)],
        "directives": [
            {
                "id": made,
                "kind": "generator-correction",
                "text": MCQ,
                "scope": "course",
                "applies_to": "I3302",
            }
        ],
    }


def test_a_gap_of_exactly_zero_is_no_room(service_engine, user_id, i3304):
    directive(service_engine, user_id, MCQ, status="proposed")

    assert work(service_engine, user_id, {"gap": 0.0}) == EMPTY


def test_no_exam_ahead_is_unknown_room_and_lists_nothing(service_engine, user_id, i3304):
    directive(service_engine, user_id, MCQ, status="proposed")

    assert work(service_engine, user_id, {"gap": None}) == EMPTY


def test_a_surplus_with_nothing_to_list_sends_two_empty_lists(service_engine, user_id):
    assert work(service_engine, user_id, ROOM) == EMPTY


# --- which papers count --------------------------------------------------------------------------


def test_only_past_papers_at_estimate_zero_are_listed(service_engine, user_id):
    i3301 = _course(service_engine, user_id, "I3301")
    i3304 = _course(service_engine, user_id, "I3304")
    _plan(
        service_engine,
        user_id,
        [
            (i3301, "first-material", i3301, 400.0, True, "I3301: feed a chapter"),
            (i3301, "past-papers", "slot-a", 120.0, True, "I3301 Partial exam: get more papers"),
            (i3304, "more-papers", "slot-final", 1.63, False, "I3304 Final exam: more papers"),
            (i3304, "past-papers", "slot-partial", 0.0, False, ZERO_REASON),
        ],
    )

    assert work(service_engine, user_id, ROOM)["papers"] == [PAPER(i3304)]


def test_a_hidden_zero_paper_is_still_listed(service_engine, user_id, i3304):
    """Estimate 0 ranks last in Setup, so the item is never `shown`, and /state's plan section
    never sends it. The rule must read it anyway."""
    with service_engine.connect() as conn:
        shown = conn.execute(select(plan_item.c.shown)).scalar_one()
    assert shown == 0

    assert work(service_engine, user_id, ROOM)["papers"] == [PAPER(i3304)]


def test_only_the_latest_all_courses_plan_counts_and_a_dropped_course_leaves(
    service_engine, user_id, i3304
):
    """D-163: the plan /state shows. An older plan's item and a one-course plan's item stay out;
    so does a course dropped after the plan was built (D-217)."""
    i3306 = _course(service_engine, user_id, "I3306")
    _plan(service_engine, user_id, [(i3306, "past-papers", "old", 0.0, False, "older plan")])
    latest = [
        (i3304, "past-papers", "slot-partial", 0.0, False, ZERO_REASON),
        (i3306, "past-papers", "slot-i3306", 0.0, False, "I3306 Partial exam: papers"),
    ]
    _plan(service_engine, user_id, latest)
    _plan(
        service_engine,
        user_id,
        [(i3306, "past-papers", "one-course", 0.0, False, "a one-course plan")],
        scope="course",
        scope_course_id=i3306,
    )
    drop_course(service_engine, user_id, "I3306")

    assert work(service_engine, user_id, ROOM)["papers"] == [PAPER(i3304)]


# --- which directives count ----------------------------------------------------------------------


def test_only_his_proposed_directives_are_listed_oldest_first(
    service_engine, user_id, other_user_id
):
    _course(service_engine, user_id, "I3302")
    first = submit_feedback(
        service_engine, user_id, "format-preference", directive_text=MCQ, code="I3302"
    )["directive"]["id"]
    every = submit_feedback(
        service_engine,
        user_id,
        "generator-correction",
        directive_text="Never ask about deprecated mysql_* functions.",
        scope="all",
    )["directive"]["id"]
    directive(service_engine, user_id, "already active", status="active")
    directive(service_engine, user_id, "replaced", status="superseded")
    directive(service_engine, other_user_id, "someone else's", status="proposed")

    listed = work(service_engine, user_id, ROOM)["directives"]

    assert [(d["id"], d["scope"], d["applies_to"]) for d in listed] == [
        (first, "course", "I3302"),
        (every, "all", "all courses"),
    ]


# --- the page's write: POST /directives/confirm --------------------------------------------------


@pytest.fixture
def client(service_engine, user_id):
    return TestClient(create_app(service_engine), base_url=OWN)


def _row(engine, directive_id):
    with engine.connect() as conn:
        return conn.execute(
            select(generation_directive.c.status, generation_directive.c.confirmed_at).where(
                generation_directive.c.id == directive_id
            )
        ).one()


def test_confirming_from_the_page_writes_the_row_the_tool_writes(
    client, call, service_engine, user_id
):
    by_page = directive(service_engine, user_id, MCQ, status="proposed")
    by_tool = directive(service_engine, user_id, "Another rule.", status="proposed")

    response = client.post("/directives/confirm", json={"directive_id": by_page})
    tool = call("study_confirm_directive", {"directive_id": by_tool})

    assert response.status_code == 200
    assert response.json().keys() == tool.structured_content.keys()
    page_row, tool_row = _row(service_engine, by_page), _row(service_engine, by_tool)
    assert page_row.status == tool_row.status == "active"
    assert page_row.confirmed_at is not None and tool_row.confirmed_at is not None

    again = client.post("/directives/confirm", json={"directive_id": by_page})
    tool_again = call("study_confirm_directive", {"directive_id": by_tool})
    assert again.status_code == 404
    assert again.json() == tool_again.structured_content  # not_found, the tool's shape


@pytest.mark.parametrize(
    ("body", "field"),
    [
        ({}, "directive_id"),
        ({"directive_id": 12}, "directive_id"),
        ({"directive_id": "x", "status": "active"}, "status"),
    ],
)
def test_the_confirm_route_checks_its_fields(client, service_engine, user_id, body, field):
    proposed = directive(service_engine, user_id, MCQ, status="proposed")

    response = client.post("/directives/confirm", json=body)

    assert response.status_code == 400
    assert response.json()["field_errors"][0]["field"] == field
    assert _row(service_engine, proposed).status == "proposed"


# --- /state carries the section ------------------------------------------------------------------


def test_state_sends_the_section_and_with_no_exam_ahead_it_is_empty(service_engine, user_id, i3304):
    """add_course's final has no date, so no exam is ahead: room is unknown (D-310)."""
    directive(service_engine, user_id, MCQ, status="proposed")

    blob = state.get_state(service_engine, user_id, datetime.date(2026, 11, 19), "t")

    assert blob["system_work"] == EMPTY
