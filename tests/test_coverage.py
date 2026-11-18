"""Coverage-at-exam (4.6; D-268 - D-271). The eight cases Mohammad picked: five on the pure
`course_reading` with hand-made entries, three through a real plan run."""

import pytest
from sqlalchemy import func, select, update

from studysystem.db.tables import course, coverage_reading, plan
from studysystem.services import state
from studysystem.services.coverage import course_reading
from studysystem.services.ids import now
from studysystem.services.plan import get_plan
from studysystem.services.units import write_unit
from tests.test_plan import TODAY, _course, _edge, _exam, _profile, _topic
from tests.test_unconfirmed import _hours_on


def _on(assessment_id, days, share, source, name=None):
    """One exam entry of a topic, as `plan._topic_exams` builds it (only the fields read here)."""
    return {
        "assessment_id": assessment_id,
        "exam_name": name or assessment_id,
        "days": days,
        "share": share,
        "share_source": source,
    }


def _entry(topic_id, *exams):
    return {"topic_id": topic_id, "exams": list(exams)}


# I3304's Partial on 2026-11-12 (profile v1): {topic: (share, reached)}.
I3304_PARTIAL = {
    "TCP congestion": (43.2, True),
    "NAT": (20.0, False),
    "Distance-vector routing": (13.7, False),
    "Frame hex decoding": (7.8, True),
    "HTTP": (7.2, True),
    "TCP reliable transfer": (5.9, True),
    "TCP vs UDP": (2.0, True),
}


# --- course_reading (his piece) --------------------------------------------------------------


def test_1_two_exams_the_nearer_one_is_measured():
    # I3304's shape: both topics on both exams. The Final would read 60%, the Partial reads 80%.
    entries = [
        _entry(
            "tcp",
            _on("final", 67, 60.0, "profile", "Final exam"),
            _on("partial", 25, 80.0, "profile", "Partial exam"),
        ),
        _entry(
            "nat",
            _on("final", 67, 40.0, "profile", "Final exam"),
            _on("partial", 25, 20.0, "profile", "Partial exam"),
        ),
    ]

    reading = course_reading(entries, unreached={"nat"}, unexamined=0)

    assert reading["value"] == pytest.approx(0.8)
    assert reading["tier_note"].startswith("Partial exam:")


def test_2_the_value_is_reached_shares_over_all_shares():
    entries = [
        _entry(name, _on("partial", 25, share, "profile", "Partial exam"))
        for name, (share, _) in I3304_PARTIAL.items()
    ]
    unreached = {name for name, (_, reached) in I3304_PARTIAL.items() if not reached}

    reading = course_reading(entries, unreached, unexamined=0)

    assert reading == {
        "value": pytest.approx(66.1 / 99.8),
        "denominator_tier": "weighted",
        "tier_note": "Partial exam: 7 profile; 0 unexamined topics excluded",
    }


def test_3_nothing_reached_is_zero_not_unknown():
    # I3305: four topics on its Partial, an equal split, none reached.
    names = ["a", "b", "c", "d"]
    entries = [_entry(n, _on("partial", 25, 25.0, "equal split", "Partial exam")) for n in names]

    reading = course_reading(entries, unreached=set(names), unexamined=0)

    assert reading is not None
    assert reading["value"] == 0.0
    assert reading["denominator_tier"] == "unweighted"


def test_4_mixed_sources_the_most_shares_wins_and_the_note_says_the_mix():
    # 70 from the profile beats 3 x 10 guessed (D-271).
    entries = [_entry("big", _on("final", 67, 70.0, "profile", "Final exam"))] + [
        _entry(n, _on("final", 67, 10.0, "average", "Final exam")) for n in ("x", "y", "z")
    ]

    reading = course_reading(entries, unreached=set(), unexamined=2)

    assert reading["denominator_tier"] == "weighted"
    note = reading["tier_note"]
    assert note.startswith("Final exam:")
    assert "1 profile" in note
    assert "3 average" in note
    assert note.endswith("; 2 unexamined topics excluded")


@pytest.mark.parametrize(
    ("first", "second", "tier"),
    [
        ("profile", "equal split", "unweighted"),
        ("profile", "average", "inferred"),
        ("average", "equal split", "unweighted"),
        ("stated", "equal split", "unweighted"),
    ],
)
def test_5_a_tie_goes_to_the_weaker_tier(first, second, tier):
    entries = [
        _entry("a", _on("final", 67, 50.0, first, "Final exam")),
        _entry("b", _on("final", 67, 50.0, second, "Final exam")),
    ]

    assert course_reading(entries, unreached=set(), unexamined=0)["denominator_tier"] == tier


def test_9_every_share_zero_reads_zero_not_a_crash():
    # D-272: a profile or a pin can give every topic 0. Stored as 0%, the plan run goes on.
    entries = [_entry("regex", _on("final", 67, 0.0, "profile", "Final exam"))]

    reading = course_reading(entries, unreached=set(), unexamined=0)

    assert reading["value"] == 0.0
    assert reading["denominator_tier"] == "weighted"


def test_6_no_topics_no_reading():
    assert course_reading([], unreached=set(), unexamined=0) is None


# --- through a plan run ----------------------------------------------------------------------


def _readings(engine, course_id):
    with engine.connect() as conn:
        return conn.execute(
            select(coverage_reading)
            .where(coverage_reading.c.course_id == course_id)
            .order_by(coverage_reading.c.id)
        ).all()


def test_6b_a_course_with_no_topics_gets_no_row(service_engine, user_id):
    # DHR300: exams, no topics.
    with write_unit(service_engine) as conn:
        dhr = _course(conn, user_id, "DHR300")
        _exam(conn, user_id, dhr)

    get_plan(service_engine, user_id, TODAY)

    assert _readings(service_engine, dhr) == []


def test_7_a_dropped_course_gets_no_row_and_a_live_one_does(service_engine, user_id):
    # I3350, dropped, still has a topic on its Final - `_study_rows` returns it (D-217).
    with write_unit(service_engine) as conn:
        dropped = _course(conn, user_id, "I3350")
        _, final = _exam(conn, user_id, dropped)
        _edge(conn, user_id, _topic(conn, user_id, dropped, "SQL"), final)
        conn.execute(update(course).where(course.c.id == dropped).values(dropped_at=now()))

        live = _course(conn, user_id, "I3302")
        slot, live_final = _exam(conn, user_id, live)
        mysql = _topic(conn, user_id, live, "MySQL")
        _edge(conn, user_id, mysql, live_final)
        _profile(conn, user_id, slot, 1, {mysql: 100.0})
        _topic(conn, user_id, live, "Old syllabus line", status="unexamined")

    get_plan(service_engine, user_id, TODAY)

    assert _readings(service_engine, dropped) == []
    [row] = _readings(service_engine, live)
    assert row.value == 0.0
    assert row.denominator_tier == "weighted"
    assert row.tier_note == "Final exam: 1 profile; 1 unexamined topic excluded"


def test_8_insert_only_two_runs_two_rows_and_the_card_shows_the_newer(service_engine, user_id):
    with write_unit(service_engine) as conn:
        i3302 = _course(conn, user_id, "I3302")
        slot, final = _exam(conn, user_id, i3302)
        mysql = _topic(conn, user_id, i3302, "MySQL")
        _edge(conn, user_id, mysql, final)
        _profile(conn, user_id, slot, 1, {mysql: 100.0})

    get_plan(service_engine, user_id, TODAY)  # nothing reached: 0%
    with write_unit(service_engine) as conn:
        _hours_on(conn, user_id, i3302, mysql)  # now reached
    get_plan(service_engine, user_id, TODAY)  # 100%

    first, second = _readings(service_engine, i3302)
    assert (first.value, second.value) == (0.0, 1.0)  # the first row is untouched
    with service_engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(coverage_reading)).scalar() == 2
        [card] = [c for c in state._courses(conn, user_id, TODAY) if c["course_id"] == i3302]
    assert card["coverage"] == {
        "value": 1.0,
        "denominator_tier": "weighted",
        "tier_note": "Final exam: 1 profile; 0 unexamined topics excluded",
        "computed_at": second.computed_at,
    }
    assert card["coverage_tier"] == "inferred"


def test_8b_a_course_with_no_reading_shows_coverage_unknown(service_engine, user_id):
    # D-269: no reading yet reads unknown, never 0.
    with write_unit(service_engine) as conn:
        dhr = _course(conn, user_id, "DHR300")
    with service_engine.connect() as conn:
        [card] = [c for c in state._courses(conn, user_id, TODAY) if c["course_id"] == dhr]
    assert (card["coverage"], card["coverage_tier"]) == (None, "unknown")


def test_10_one_run_writes_each_course_its_own_reading(service_engine, user_id, other_user_id):
    # I3302 reached, I3304 not, in one plan run; another student's course never gets his row.
    with write_unit(service_engine) as conn:
        courses = {}
        for code in ("I3302", "I3304"):
            course_id = _course(conn, user_id, code)
            slot, final = _exam(conn, user_id, course_id)
            topic_id = _topic(conn, user_id, course_id, f"{code} topic")
            _edge(conn, user_id, topic_id, final)
            _profile(conn, user_id, slot, 1, {topic_id: 100.0})
            courses[code] = (course_id, topic_id)
        _hours_on(conn, user_id, *courses["I3302"])
        theirs = _course(conn, other_user_id, "I3302")

    get_plan(service_engine, user_id, TODAY)

    [i3302] = _readings(service_engine, courses["I3302"][0])
    [i3304] = _readings(service_engine, courses["I3304"][0])
    assert (i3302.value, i3304.value) == (1.0, 0.0)
    assert (i3302.user_id, i3304.user_id) == (user_id, user_id)
    assert _readings(service_engine, theirs) == []


def test_11_reading_the_state_writes_no_reading(service_engine, user_id):
    # D-268: only a plan run writes; GET /state reads.
    with write_unit(service_engine) as conn:
        course_id = _course(conn, user_id, "I3302")
        slot, final = _exam(conn, user_id, course_id)
        topic_id = _topic(conn, user_id, course_id, "MySQL")
        _edge(conn, user_id, topic_id, final)
        _profile(conn, user_id, slot, 1, {topic_id: 100.0})

    state.get_state(service_engine, user_id, TODAY, "test")
    state.get_state(service_engine, user_id, TODAY, "test")

    assert _readings(service_engine, course_id) == []


def test_get_plan_stamps_the_plan_and_its_readings_with_the_clock_it_is_given(
    service_engine, user_id
):
    # D-308: only the demo seed passes `now`; the default stays the real clock.
    with write_unit(service_engine) as conn:
        i3302 = _course(conn, user_id, "I3302")
        slot, final = _exam(conn, user_id, i3302)
        mysql = _topic(conn, user_id, i3302, "MySQL")
        _edge(conn, user_id, mysql, final)
        _profile(conn, user_id, slot, 1, {mysql: 100.0})
    clock = "2026-11-02T08:00:00Z"

    given = get_plan(service_engine, user_id, TODAY, now=clock)
    before = now()
    real = get_plan(service_engine, user_id, TODAY)
    after = now()

    with service_engine.connect() as conn:
        stamps = dict(conn.execute(select(plan.c.id, plan.c.created_at)).all())
    assert stamps[given["plan_id"]] == clock
    assert before <= stamps[real["plan_id"]] <= after
    first, second = _readings(service_engine, i3302)
    assert first.computed_at == clock
    assert before <= second.computed_at <= after
