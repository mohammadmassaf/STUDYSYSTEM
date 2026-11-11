"""The `unconfirmed` flag (4.5; D-266, D-267): untagged taught chapters, unreached topics, and the
per-course pairing of the two. Fixture rows built directly - each test makes only what its case
needs."""

import datetime

import pytest
from sqlalchemy import insert, update

from studysystem.db.tables import attempt, hours_entry, note
from studysystem.services import state
from studysystem.services.ids import new_id, now
from studysystem.services.unconfirmed import (
    unconfirmed_by_course,
    unreached_topics,
    untagged_taught_chapters,
)
from studysystem.services.units import write_unit
from tests.test_plan import (
    _chapter,
    _course,
    _edge,
    _exam,
    _noted,
    _studied,
    _tagged,
    _topic,
    _tried,
)


def _hours_on(conn, uid, course_id, topic_id, voided_at=None):
    conn.execute(
        insert(hours_entry).values(
            id=new_id(),
            user_id=uid,
            course_id=course_id,
            topic_id=topic_id,
            minutes=20,
            occurred_at=now(),
            source="tool",
            voided_at=voided_at,
            created_at=now(),
        )
    )


def _chapters(engine, uid) -> set[str]:
    with engine.connect() as conn:
        return {row.id for row in conn.execute(untagged_taught_chapters(uid))}


def _unreached(engine, uid) -> set[str]:
    with engine.connect() as conn:
        return {row.id for row in conn.execute(unreached_topics(uid))}


# --- untagged_taught_chapters (his piece) ----------------------------------------------------


def test_a_noted_chapter_with_no_edge_is_listed_with_its_course_and_file(service_engine, user_id):
    # I3304's Chapter 7. NAT's exam edge sits in the same table and must not tag the chapter.
    with write_unit(service_engine) as conn:
        course_id = _course(conn, user_id, "I3304")
        _, final = _exam(conn, user_id, course_id)
        _edge(conn, user_id, _topic(conn, user_id, course_id, "NAT"), final)
        ch7 = _chapter(conn, user_id, course_id, filename="Chapter7_final-LAN Redundancy.pdf")
        _noted(conn, user_id, ch7)

    with service_engine.connect() as conn:
        rows = conn.execute(untagged_taught_chapters(user_id)).all()

    assert [(r.id, r.course_id, r.filename) for r in rows] == [
        (ch7, course_id, "Chapter7_final-LAN Redundancy.pdf")
    ]


@pytest.mark.parametrize("status", ["active", "unexamined"])
def test_a_chapter_tagged_to_a_live_topic_is_not_listed(service_engine, user_id, status):
    with write_unit(service_engine) as conn:
        course_id = _course(conn, user_id, "I3304")
        http = _chapter(conn, user_id, course_id)
        _noted(conn, user_id, http)
        _tagged(conn, user_id, _topic(conn, user_id, course_id, "HTTP", status), http)

    assert _chapters(service_engine, user_id) == set()


@pytest.mark.parametrize("status", ["superseded", "declined"])
def test_a_chapter_tagged_only_to_a_dead_topic_is_listed(service_engine, user_id, status):
    with write_unit(service_engine) as conn:
        course_id = _course(conn, user_id, "I3304")
        chapter = _chapter(conn, user_id, course_id)
        _noted(conn, user_id, chapter)
        _tagged(conn, user_id, _topic(conn, user_id, course_id, "old name", status), chapter)

    assert _chapters(service_engine, user_id) == {chapter}


def test_a_chapter_with_a_session_and_no_note_is_listed(service_engine, user_id):
    with write_unit(service_engine) as conn:
        course_id = _course(conn, user_id, "I3304")
        chapter = _chapter(conn, user_id, course_id)
        _studied(conn, user_id, course_id, chapter)

    assert _chapters(service_engine, user_id) == {chapter}


def test_a_chapter_whose_only_note_is_retired_is_not_taught(service_engine, user_id):
    with write_unit(service_engine) as conn:
        course_id = _course(conn, user_id, "I3304")
        chapter = _chapter(conn, user_id, course_id)
        _noted(conn, user_id, chapter)
        conn.execute(update(note).where(note.c.material_id == chapter).values(retired_at=now()))

    assert _chapters(service_engine, user_id) == set()


def test_a_chapter_fed_but_never_taught_is_not_listed(service_engine, user_id):
    with write_unit(service_engine) as conn:
        _chapter(conn, user_id, _course(conn, user_id, "I3305"))

    assert _chapters(service_engine, user_id) == set()


def test_another_users_chapter_is_not_listed(service_engine, user_id, other_user_id):
    with write_unit(service_engine) as conn:
        course_id = _course(conn, other_user_id, "I3304")
        chapter = _chapter(conn, other_user_id, course_id)
        _noted(conn, other_user_id, chapter)

    assert _chapters(service_engine, user_id) == set()
    assert _chapters(service_engine, other_user_id) == {chapter}


# --- unreached_topics --------------------------------------------------------------------------


def test_live_hours_reach_a_topic_and_voided_hours_do_not(service_engine, user_id):
    with write_unit(service_engine) as conn:
        course_id = _course(conn, user_id, "I3304")
        layers = _topic(conn, user_id, course_id, "Protocol layers")
        nat = _topic(conn, user_id, course_id, "NAT")
        _hours_on(conn, user_id, course_id, layers)
        _hours_on(conn, user_id, course_id, nat, voided_at=now())

    assert _unreached(service_engine, user_id) == {nat}


def test_an_attempt_reaches_a_topic_even_with_voided_correctness(service_engine, user_id):
    with write_unit(service_engine) as conn:
        course_id = _course(conn, user_id, "I3304")
        nat = _topic(conn, user_id, course_id, "NAT")
        _tried(conn, user_id, nat, None)
        conn.execute(update(attempt).values(correctness_voided=1))

    assert _unreached(service_engine, user_id) == set()


def test_a_taught_chapter_reaches_its_topics_and_an_untaught_one_does_not(service_engine, user_id):
    with write_unit(service_engine) as conn:
        course_id = _course(conn, user_id, "I3304")
        layers = _topic(conn, user_id, course_id, "Protocol layers")
        congestion = _topic(conn, user_id, course_id, "TCP congestion")
        ch1, ch2 = _chapter(conn, user_id, course_id), _chapter(conn, user_id, course_id)
        _noted(conn, user_id, ch1)
        _tagged(conn, user_id, layers, ch1)
        _tagged(conn, user_id, congestion, ch2)  # fed ahead of its lecture

    assert _unreached(service_engine, user_id) == {congestion}


def test_an_exam_edge_alone_reaches_nothing(service_engine, user_id):
    # VLANs: on the Final, on no chapter.
    with write_unit(service_engine) as conn:
        course_id = _course(conn, user_id, "I3304")
        _, final = _exam(conn, user_id, course_id)
        vlans = _topic(conn, user_id, course_id, "VLANs")
        _edge(conn, user_id, vlans, final)

    assert _unreached(service_engine, user_id) == {vlans}


@pytest.mark.parametrize("status", ["proposed", "unexamined", "superseded", "declined"])
def test_only_active_topics_are_listed(service_engine, user_id, status):
    with write_unit(service_engine) as conn:
        _topic(conn, user_id, _course(conn, user_id, "I3304"), "NAT", status)

    assert _unreached(service_engine, user_id) == set()


def test_another_users_topic_is_not_listed(service_engine, user_id, other_user_id):
    with write_unit(service_engine) as conn:
        _topic(conn, other_user_id, _course(conn, other_user_id, "I3304"), "NAT")

    assert _unreached(service_engine, user_id) == set()


# --- unconfirmed_by_course ---------------------------------------------------------------------


def _flags(engine, uid) -> dict:
    with engine.connect() as conn:
        return unconfirmed_by_course(conn, uid)


def test_i3304_pairs_its_untagged_chapter_with_its_unreached_topics(service_engine, user_id):
    with write_unit(service_engine) as conn:
        course_id = _course(conn, user_id, "I3304")
        layers = _topic(conn, user_id, course_id, "Protocol layers")
        vlans = _topic(conn, user_id, course_id, "VLANs")
        nat = _topic(conn, user_id, course_id, "NAT")
        ch1 = _chapter(conn, user_id, course_id, filename="chapter 1.pdf")
        ch7 = _chapter(conn, user_id, course_id, filename="Chapter7 LAN Redundancy.pdf")
        _noted(conn, user_id, ch1)
        _noted(conn, user_id, ch7)
        _tagged(conn, user_id, layers, ch1)

    assert _flags(service_engine, user_id) == {
        course_id: {
            "chapters": [{"material_id": ch7, "chapter": "Chapter7 LAN Redundancy"}],
            "topics": [{"topic_id": nat, "name": "NAT"}, {"topic_id": vlans, "name": "VLANs"}],
        }
    }


def test_a_course_with_no_untagged_chapter_has_no_flag(service_engine, user_id):
    # I3303's shape: many unreached topics, but its one taught chapter is tagged.
    with write_unit(service_engine) as conn:
        course_id = _course(conn, user_id, "I3303")
        pipes = _topic(conn, user_id, course_id, "Pipes")
        _topic(conn, user_id, course_id, "Signals")
        chapter = _chapter(conn, user_id, course_id)
        _noted(conn, user_id, chapter)
        _tagged(conn, user_id, pipes, chapter)

    assert _flags(service_engine, user_id) == {}


def test_a_course_with_every_topic_reached_has_no_flag(service_engine, user_id):
    with write_unit(service_engine) as conn:
        course_id = _course(conn, user_id, "I3302")
        php = _topic(conn, user_id, course_id, "PHP")
        intro, ch2 = _chapter(conn, user_id, course_id), _chapter(conn, user_id, course_id)
        _noted(conn, user_id, intro)  # untagged
        _noted(conn, user_id, ch2)
        _tagged(conn, user_id, php, ch2)

    assert _flags(service_engine, user_id) == {}


# --- GET /state ----------------------------------------------------------------------------------


def test_the_courses_section_carries_the_flag_and_empty_lists_elsewhere(service_engine, user_id):
    # Done when: the dashboard surfaces it. A course with no flag sends two empty lists, not null.
    with write_unit(service_engine) as conn:
        i3304 = _course(conn, user_id, "I3304")
        nat = _topic(conn, user_id, i3304, "NAT")
        ch7 = _chapter(conn, user_id, i3304, filename="Chapter7 LAN Redundancy.pdf")
        _noted(conn, user_id, ch7)
        _topic(conn, user_id, _course(conn, user_id, "I3305"), "Creational patterns")

    courses = state.get_state(service_engine, user_id, datetime.date(2026, 11, 11), "0.1.0")[
        "courses"
    ]

    flags = {c["code"]: c["unconfirmed"] for c in courses}
    assert flags == {
        "I3304": {
            "chapters": [{"material_id": ch7, "chapter": "Chapter7 LAN Redundancy"}],
            "topics": [{"topic_id": nat, "name": "NAT"}],
        },
        "I3305": {"chapters": [], "topics": []},
    }
