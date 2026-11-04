"""The dogfood gate (1.17; D-21, D-113, D-116, D-117) on both engines. Each test builds plans and
hours with fixed timestamps, shaped on his live database: plans P1-P6 from 2026-10-05 (P1 and P2
in the same second), MySQL as study #1, I3350's first-material as setup #1, I3305's as hidden
setup #6."""

import pytest
from sqlalchemy import insert

from studysystem.db.tables import hours_entry, plan, plan_item, topic
from studysystem.server import create_server
from studysystem.services.courses import add_course
from studysystem.services.gate import dogfood_gate
from studysystem.services.ids import new_id, now
from studysystem.services.plan import get_plan
from studysystem.services.study_loop import log_hours, start_session
from studysystem.services.units import write_unit
from tests._client import tool_names
from tests.test_plan import (
    TODAY,
    _chapter,
    _i3302_and_a_cold_course,
    _i3304_transport,
    _tagged,
)

SEM = "Semester 1 2026-2027"


@pytest.fixture
def web(service_engine, user_id):
    return add_course(service_engine, user_id, "I3302", "Server-Side Web Development", SEM)[
        "course_id"
    ]


@pytest.fixture
def mysql(service_engine, user_id, web):
    return _topic(service_engine, user_id, web, "PHP + MySQL (prepared statements)")


def _topic(engine, uid, course_id, name):
    topic_id = new_id()
    with write_unit(engine) as conn:
        conn.execute(
            insert(topic).values(
                id=topic_id,
                user_id=uid,
                course_id=course_id,
                name=name,
                status="active",
                proposed_by="profile",
                status_changed_at=now(),
                created_at=now(),
            )
        )
    return topic_id


def _plan(engine, uid, created_at):
    plan_id = new_id()
    with write_unit(engine) as conn:
        conn.execute(
            insert(plan).values(
                id=plan_id,
                user_id=uid,
                scope="all-courses",
                shown_n=5,
                strategy_snapshot="{}",
                created_at=created_at,
            )
        )
    return plan_id


def _study_item(engine, uid, plan_id, course_id, topic_id, rank, shown, material_id=None):
    item_id = new_id()
    with write_unit(engine) as conn:
        conn.execute(
            insert(plan_item).values(
                id=item_id,
                user_id=uid,
                plan_id=plan_id,
                lane="study",
                course_id=course_id,
                topic_id=topic_id,
                material_id=material_id,
                rank=rank,
                shown=int(shown),
                score=1.0,
                reason=f"study #{rank}",
                estimated_minutes=60,
            )
        )
    return item_id


def _setup_item(engine, uid, plan_id, course_id, rank, shown):
    item_id = new_id()
    with write_unit(engine) as conn:
        conn.execute(
            insert(plan_item).values(
                id=item_id,
                user_id=uid,
                plan_id=plan_id,
                lane="setup",
                course_id=course_id,
                setup_kind="first-material",
                setup_key=f"first-material:{course_id}",
                rank=rank,
                shown=int(shown),
                reason=f"setup #{rank}",
            )
        )
    return item_id


def _hours(
    engine,
    uid,
    course_id,
    occurred_at,
    *,
    topic_id=None,
    setup_key=None,
    material_id=None,
    voided=False,
):
    entry_id = new_id()
    with write_unit(engine) as conn:
        conn.execute(
            insert(hours_entry).values(
                id=entry_id,
                user_id=uid,
                course_id=course_id,
                topic_id=topic_id,
                setup_key=setup_key,
                material_id=material_id,
                minutes=60,
                occurred_at=occurred_at,
                source="tool",
                voided_at="2026-10-09T21:00:00Z" if voided else None,
                created_at=now(),
            )
        )
    return entry_id


def _course(engine, uid, code):
    return add_course(engine, uid, code, code, SEM)["course_id"]


# --- 1-10: the cases he picked -----------------------------------------------------------------


def test_1_no_hours_is_not_used(service_engine, user_id, web, mysql):
    p6 = _plan(service_engine, user_id, "2026-10-05T18:54:00Z")
    _study_item(service_engine, user_id, p6, web, mysql, 1, shown=True)

    assert dogfood_gate(service_engine, user_id) == {"used": False, "rows": []}


def test_2_an_hour_on_a_shown_topic_counts(service_engine, user_id, web, mysql):
    p7 = _plan(service_engine, user_id, "2026-10-09T19:00:00Z")
    item = _study_item(service_engine, user_id, p7, web, mysql, 1, shown=True)
    entry = _hours(service_engine, user_id, web, "2026-10-09T19:10:00Z", topic_id=mysql)

    assert dogfood_gate(service_engine, user_id) == {
        "used": True,
        "rows": [
            {
                "hours_entry_id": entry,
                "course_id": web,
                "topic_id": mysql,
                "setup_key": None,
                "material_id": None,
                "minutes": 60,
                "occurred_at": "2026-10-09T19:10:00Z",
                "plan_id": p7,
                "plan_item_id": item,
                "lane": "study",
                "rank": 1,
                "reason": "study #1",
            }
        ],
    }


def test_3_an_hour_on_a_hidden_setup_item_does_not_count(service_engine, user_id):
    i3305 = _course(service_engine, user_id, "I3305")
    p6 = _plan(service_engine, user_id, "2026-10-05T18:54:00Z")
    _setup_item(service_engine, user_id, p6, i3305, 6, shown=False)
    key = f"first-material:{i3305}"
    _hours(service_engine, user_id, i3305, "2026-10-09T19:10:00Z", setup_key=key)

    assert dogfood_gate(service_engine, user_id)["used"] is False


def test_4_a_voided_hour_does_not_count(service_engine, user_id, web, mysql):
    p7 = _plan(service_engine, user_id, "2026-10-09T19:00:00Z")
    _study_item(service_engine, user_id, p7, web, mysql, 1, shown=True)
    _hours(service_engine, user_id, web, "2026-10-09T19:10:00Z", topic_id=mysql, voided=True)

    assert dogfood_gate(service_engine, user_id) == {"used": False, "rows": []}


def test_5_a_course_level_hour_matches_nothing(service_engine, user_id, web, mysql):
    p7 = _plan(service_engine, user_id, "2026-10-09T19:00:00Z")
    _study_item(service_engine, user_id, p7, web, mysql, 1, shown=True)
    _setup_item(service_engine, user_id, p7, web, 1, shown=True)
    _hours(service_engine, user_id, web, "2026-10-09T19:10:00Z")  # no topic, no setup key

    assert dogfood_gate(service_engine, user_id)["used"] is False


def test_6_an_hour_before_any_plan_has_no_plan_in_force(service_engine, user_id, web, mysql):
    p1 = _plan(service_engine, user_id, "2026-10-05T08:42:26Z")
    _study_item(service_engine, user_id, p1, web, mysql, 1, shown=True)
    _hours(service_engine, user_id, web, "2026-10-04T19:00:00Z", topic_id=mysql)

    assert dogfood_gate(service_engine, user_id)["used"] is False


def test_7_shown_in_an_older_plan_only_does_not_count(service_engine, user_id, web, mysql):
    """The P1 trap: MySQL was shown in P1, but the plan in force (P6) ranked it hidden."""
    p1 = _plan(service_engine, user_id, "2026-10-05T08:42:26Z")
    _study_item(service_engine, user_id, p1, web, mysql, 1, shown=True)
    p6 = _plan(service_engine, user_id, "2026-10-05T18:54:00Z")
    _study_item(service_engine, user_id, p6, web, mysql, 6, shown=False)
    _hours(service_engine, user_id, web, "2026-10-09T18:00:00Z", topic_id=mysql)

    assert dogfood_gate(service_engine, user_id)["used"] is False


def test_8_an_hour_on_a_shown_setup_key_counts(service_engine, user_id):
    i3350 = _course(service_engine, user_id, "I3350")
    p6 = _plan(service_engine, user_id, "2026-10-05T18:54:00Z")
    item = _setup_item(service_engine, user_id, p6, i3350, 1, shown=True)
    key = f"first-material:{i3350}"
    entry = _hours(service_engine, user_id, i3350, "2026-10-09T19:10:00Z", setup_key=key)

    gate = dogfood_gate(service_engine, user_id)
    assert gate["used"] is True
    [row] = gate["rows"]
    assert (row["hours_entry_id"], row["plan_item_id"]) == (entry, item)
    assert (row["lane"], row["setup_key"], row["topic_id"]) == ("setup", key, None)


def test_9_a_plan_made_after_the_hour_is_ignored(service_engine, user_id, web, mysql):
    """P7 at 19:00 showed MySQL; P8 at 21:00 hid it. The 19:10 hour answers to P7."""
    p7 = _plan(service_engine, user_id, "2026-10-09T19:00:00Z")
    item = _study_item(service_engine, user_id, p7, web, mysql, 1, shown=True)
    p8 = _plan(service_engine, user_id, "2026-10-09T21:00:00Z")
    _study_item(service_engine, user_id, p8, web, mysql, 6, shown=False)
    _hours(service_engine, user_id, web, "2026-10-09T19:10:00Z", topic_id=mysql)

    [row] = dogfood_gate(service_engine, user_id)["rows"]
    assert (row["plan_id"], row["plan_item_id"]) == (p7, item)


def test_10_two_plans_in_one_second_the_later_id_is_in_force(service_engine, user_id, web, mysql):
    """P1 and P2 share 2026-10-05T08:42:26Z, as on his live database; P2 was made second, so
    its id sorts after P1's. Sessions is shown only in P2, MySQL only in P1: only Sessions
    counts. Any fixed pick of P1 fails this, and so does matching against both."""
    sessions = _topic(service_engine, user_id, web, "PHP sessions")
    p1 = _plan(service_engine, user_id, "2026-10-05T08:42:26Z")
    p2 = _plan(service_engine, user_id, "2026-10-05T08:42:26Z")
    assert p1 < p2
    _study_item(service_engine, user_id, p1, web, mysql, 1, shown=True)
    _study_item(service_engine, user_id, p1, web, sessions, 6, shown=False)
    _study_item(service_engine, user_id, p2, web, mysql, 6, shown=False)
    _study_item(service_engine, user_id, p2, web, sessions, 1, shown=True)
    on_sessions = _hours(service_engine, user_id, web, "2026-10-05T08:50:00Z", topic_id=sessions)
    _hours(service_engine, user_id, web, "2026-10-05T09:00:00Z", topic_id=mysql)

    [row] = dogfood_gate(service_engine, user_id)["rows"]
    assert (row["hours_entry_id"], row["plan_id"]) == (on_sessions, p2)


def test_another_users_plans_and_hours_never_reach_the_gate(
    service_engine, user_id, other_user_id, web, mysql
):
    """Ownership, both filters. Their plan at 19:05 is the newest before his 19:10 hour: read
    across users, it would displace P6 and lose his hour. Their 19:20 hour names his MySQL - a
    row the service refuses on write, so the gate's own filter is the second wall."""
    p6 = _plan(service_engine, user_id, "2026-10-05T18:54:00Z")
    _study_item(service_engine, user_id, p6, web, mysql, 1, shown=True)
    _plan(service_engine, other_user_id, "2026-10-09T19:05:00Z")
    mine = _hours(service_engine, user_id, web, "2026-10-09T19:10:00Z", topic_id=mysql)
    _hours(service_engine, other_user_id, web, "2026-10-09T19:20:00Z", topic_id=mysql)

    [row] = dogfood_gate(service_engine, user_id)["rows"]
    assert (row["hours_entry_id"], row["plan_id"]) == (mine, p6)
    assert dogfood_gate(service_engine, other_user_id) == {"used": False, "rows": []}


def test_an_hour_on_a_topic_in_two_lanes_is_listed_once_per_item(
    service_engine, user_id, web, mysql
):
    """D-117: MySQL due for review and ranked to study in one plan - review #1 and study #1 -
    gives the one hour two rows. Proven twice is still proven."""
    p7 = _plan(service_engine, user_id, "2026-10-09T19:00:00Z")
    review = new_id()
    with write_unit(service_engine) as conn:
        conn.execute(
            insert(plan_item).values(
                id=review,
                user_id=user_id,
                plan_id=p7,
                lane="review",
                course_id=web,
                topic_id=mysql,
                rank=1,
                shown=1,
                score=0.0,
                reason="review #1",
            )
        )
    study = _study_item(service_engine, user_id, p7, web, mysql, 1, shown=True)
    entry = _hours(service_engine, user_id, web, "2026-10-09T19:10:00Z", topic_id=mysql)

    gate = dogfood_gate(service_engine, user_id)
    assert gate["used"] is True
    assert sorted((r["hours_entry_id"], r["plan_item_id"]) for r in gate["rows"]) == sorted(
        [(entry, review), (entry, study)]
    )


# --- the tool ------------------------------------------------------------------------------------


def test_the_gate_tool_is_listed(service_engine):
    assert "study_get_gate" in tool_names(create_server(service_engine))


def test_the_gate_tool_returns_the_service_answer(call):
    result = call("study_get_gate", {})

    assert not result.is_error
    assert result.structured_content == {"used": False, "rows": []}


# --- a chapter's hour (D-238, D-240) -----------------------------------------------------------


def test_transport_2_picked_studied_and_logged_counts(service_engine, user_id):
    # The whole flow: the plan shows the chapter, the session starts on it, 60 minutes go on it.
    with write_unit(service_engine) as conn:
        _, chapters = _i3304_transport(conn, user_id)
    plan_id = get_plan(service_engine, user_id, TODAY)["plan_id"]
    transport = chapters["Transport (2)"]
    session = start_session(service_engine, user_id, "I3304", "practice", "material", transport)

    entry = log_hours(
        service_engine,
        user_id,
        "I3304",
        60,
        material_id=transport,
        session_id=session["session_id"],
    )

    gate = dogfood_gate(service_engine, user_id)
    assert gate["used"] is True
    [row] = gate["rows"]
    assert (row["hours_entry_id"], row["minutes"], row["material_id"]) == (
        entry["hours_entry_id"],
        60,
        transport,
    )
    assert (row["topic_id"], row["setup_key"]) == (None, None)
    assert (row["plan_id"], row["lane"], row["rank"]) == (plan_id, "study", 1)


def test_an_hour_on_a_shown_topic_with_no_chapter_counts(service_engine, user_id):
    # D-244: its entry names no chapter, so its minutes go on the topic, and the gate's topic rule
    # already proves them.
    with write_unit(service_engine) as conn:
        ids = _i3302_and_a_cold_course(conn, user_id)
    entries = get_plan(service_engine, user_id, TODAY)["lanes"]["study"]["entries"]
    assert entries[0]["material_id"] is None
    mysql = ids["MySQL"]
    session = start_session(service_engine, user_id, "I3302", "practice", "topic", mysql)

    log_hours(
        service_engine, user_id, "I3302", 30, topic_id=mysql, session_id=session["session_id"]
    )

    [row] = dogfood_gate(service_engine, user_id)["rows"]
    assert (row["topic_id"], row["material_id"], row["rank"]) == (mysql, None, 1)


def test_a_chapter_hour_lists_once_per_shown_topic_under_it(service_engine, user_id, web, mysql):
    # D-117: one hour, two proofs - and the hidden third topic of the chapter proves nothing.
    forms = _topic(service_engine, user_id, web, "PHP forms")
    regex = _topic(service_engine, user_id, web, "PHP regex")
    with write_unit(service_engine) as conn:
        chapter = _chapter(conn, user_id, web)
    p7 = _plan(service_engine, user_id, "2026-10-09T19:00:00Z")
    _study_item(service_engine, user_id, p7, web, mysql, 1, True, chapter)
    _study_item(service_engine, user_id, p7, web, forms, 2, True, chapter)
    _study_item(service_engine, user_id, p7, web, regex, 3, False, chapter)
    entry = _hours(service_engine, user_id, web, "2026-10-09T19:10:00Z", material_id=chapter)

    rows = dogfood_gate(service_engine, user_id)["rows"]

    assert sorted((row["hours_entry_id"], row["rank"]) for row in rows) == [(entry, 1), (entry, 2)]


def test_a_chapter_hour_with_only_hidden_topics_does_not_count(service_engine, user_id, web, mysql):
    with write_unit(service_engine) as conn:
        chapter = _chapter(conn, user_id, web)
    p7 = _plan(service_engine, user_id, "2026-10-09T19:00:00Z")
    _study_item(service_engine, user_id, p7, web, mysql, 11, False, chapter)
    _hours(service_engine, user_id, web, "2026-10-09T19:10:00Z", material_id=chapter)

    assert dogfood_gate(service_engine, user_id)["used"] is False


def test_a_chapter_hour_under_a_plan_stored_before_chapters_does_not_count(
    service_engine, user_id, web, mysql
):
    # D-240, accepted: the old plan's shown item names no chapter.
    with write_unit(service_engine) as conn:
        chapter = _chapter(conn, user_id, web)
    p6 = _plan(service_engine, user_id, "2026-10-05T18:54:00Z")
    _study_item(service_engine, user_id, p6, web, mysql, 1, True)
    _hours(service_engine, user_id, web, "2026-10-09T19:10:00Z", material_id=chapter)

    assert dogfood_gate(service_engine, user_id)["used"] is False


def test_a_course_level_hour_still_matches_nothing(service_engine, user_id, web, mysql):
    # NULL = NULL is never true: an item with no chapter must not match an hour with no chapter.
    p6 = _plan(service_engine, user_id, "2026-10-05T18:54:00Z")
    _study_item(service_engine, user_id, p6, web, mysql, 1, True)
    _hours(service_engine, user_id, web, "2026-10-09T19:10:00Z")

    assert dogfood_gate(service_engine, user_id)["used"] is False


def test_a_later_tagging_does_not_change_an_old_hour(service_engine, user_id):
    # D-240's reason: the grouping is the one stored when the plan was made. A later tagging links
    # TCP congestion to the older part2 chapter, its first chapter now, and the hour still counts.
    with write_unit(service_engine) as conn:
        ids, chapters = _i3304_transport(conn, user_id)
    transport = chapters["Transport (2)"]
    get_plan(service_engine, user_id, TODAY)
    session = start_session(service_engine, user_id, "I3304", "practice", "material", transport)
    log_hours(
        service_engine,
        user_id,
        "I3304",
        60,
        material_id=transport,
        session_id=session["session_id"],
    )

    with write_unit(service_engine) as conn:
        _tagged(conn, user_id, ids["TCP congestion"], chapters["part2"])

    [row] = dogfood_gate(service_engine, user_id)["rows"]
    assert row["material_id"] == transport
