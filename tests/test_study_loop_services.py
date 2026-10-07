"""The study loop's three writers (1.16; D-17, D-22): sessions, hours, attempts - on both engines.
Each test follows one real row of his MySQL evening: start a practice session, submit index.php's
parts, log the 40 minutes."""

import asyncio
import datetime
import json
import logging

import pytest
from mcp import Client
from sqlalchemy import func, insert, select, update

from studysystem.db.tables import (
    attempt,
    generation_task,
    hours_entry,
    past_exam,
    practice_item,
    study_session,
    topic,
    topic_state,
)
from studysystem.errors import StudyError
from studysystem.server import create_server
from studysystem.services.courses import add_course
from studysystem.services.ids import new_id, now
from studysystem.services.plan import get_plan
from studysystem.services.study_loop import log_hours, start_session, submit_attempts
from studysystem.services.units import write_unit

SEM = "Semester 1 2026-2027"
TODAY = datetime.date(2026, 10, 7)


@pytest.fixture
def web(service_engine, user_id):
    return add_course(service_engine, user_id, "I3302", "Server-Side Web Development", SEM)


def _topic(engine, uid, course_id, name, status="active"):
    topic_id = new_id()
    with write_unit(engine) as conn:
        conn.execute(
            insert(topic).values(
                id=topic_id,
                user_id=uid,
                course_id=course_id,
                name=name,
                status=status,
                proposed_by="profile",
                status_changed_at=now(),
                created_at=now(),
            )
        )
    return topic_id


def _items(engine, uid, topic_id, marks, paper=None):
    """One practice item per mark - the parts of one problem. With `paper`, they are that past
    paper's questions 1..n; without, generated items."""
    task_id = new_id()
    ids = [new_id() for _ in marks]
    with write_unit(engine) as conn:
        conn.execute(
            insert(generation_task).values(
                id=task_id,
                user_id=uid,
                kind="practice",
                scope_type="topic",
                scope_id=topic_id,
                route="host",
                created_at=now(),
            )
        )
        for n, (item_id, mark) in enumerate(zip(ids, marks, strict=True), start=1):
            origin = {"origin": "generated", "source_marker": "model-knowledge"}
            if paper is not None:
                origin = {"origin": "past-exam", "source_marker": "from-material"}
            conn.execute(
                insert(practice_item).values(
                    id=item_id,
                    user_id=uid,
                    topic_id=topic_id,
                    task_id=task_id,
                    item_ordinal=n,
                    past_exam_id=paper,
                    position=n if paper is not None else None,
                    question=f"part {n}",
                    answer_provenance="none",
                    marks=mark,
                    novelty_score=None if paper is not None else 0.5,
                    created_at=now(),
                    **origin,
                )
            )
    return ids


def _paper(engine, uid, slot_id):
    paper_id = new_id()
    with write_unit(engine) as conn:
        conn.execute(
            insert(past_exam).values(
                id=paper_id,
                owner_id=uid,
                slot_id=slot_id,
                session_type="first",
                session_date="2018-07-21",
                instructor_tier="unknown",
                file_ref="files/x",
                content_sha256="a" * 64,
                created_at=now(),
            )
        )
    return paper_id


def _count(engine, table):
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def _state(engine, topic_id):
    with engine.connect() as conn:
        return conn.execute(select(topic_state).where(topic_state.c.topic_id == topic_id)).first()


def _index_php(items):
    """Tonight's grades: 5 ok, 5 at 0.4, 15 ok, 25 wrong -> 0.44 -> Again."""
    return [
        {"practice_item_id": items[0], "correct": True},
        {"practice_item_id": items[1], "score": 0.4},
        {"practice_item_id": items[2], "correct": True},
        {"practice_item_id": items[3], "correct": False, "root_cause": "concept"},
    ]


# --- study_start_session ------------------------------------------------------------------


def test_a_session_labels_the_work_and_expires_in_four_hours(service_engine, user_id, web):
    mysql = _topic(service_engine, user_id, web["course_id"], "MySQL")

    result = start_session(service_engine, user_id, "I3302", "practice", "topic", mysql)

    with service_engine.connect() as conn:
        row = conn.execute(select(study_session)).one()
    assert (row.mode, row.subject_id, row.source, row.ended_at) == (
        "practice",
        mysql,
        "system",
        None,
    )
    started = datetime.datetime.fromisoformat(row.started_at)
    assert datetime.datetime.fromisoformat(row.expires_at) - started == datetime.timedelta(hours=4)
    assert result["session_id"] == row.id


def test_a_subject_from_another_course_is_not_found(service_engine, user_id, web):
    other = add_course(service_engine, user_id, "I3350", "Mobile", SEM)
    theirs = _topic(service_engine, user_id, other["course_id"], "Android")

    with pytest.raises(StudyError) as err:
        start_session(service_engine, user_id, "I3302", "practice", "topic", theirs)

    assert err.value.code == "not_found"
    assert _count(service_engine, study_session) == 0


def test_half_a_subject_is_refused(service_engine, user_id, web):
    with pytest.raises(StudyError) as err:
        start_session(service_engine, user_id, "I3302", "practice", subject_type="topic")
    assert err.value.field_errors[0]["field"] == "subject_id"


# --- study_submit_attempt -----------------------------------------------------------------


def test_a_problem_moves_its_topic_once(service_engine, user_id, web):
    mysql = _topic(service_engine, user_id, web["course_id"], "MySQL")
    items = _items(service_engine, user_id, mysql, [5, 5, 15, 25])
    session = start_session(service_engine, user_id, "I3302", "practice", "topic", mysql)

    result = submit_attempts(
        service_engine, user_id, "I3302", _index_php(items), session["session_id"]
    )

    state = _state(service_engine, mysql)
    assert (state.reps, state.lapses) == (1, 1)  # one Again, for four parts
    assert result["topics"] == [{"topic_id": mysql, "due_at": state.due_at, "reps": 1, "lapses": 1}]
    with service_engine.connect() as conn:
        rows = conn.execute(select(attempt.c.source, attempt.c.assisted)).all()
    assert {(r.source, bool(r.assisted)) for r in rows} == {("practice", False)}


def test_a_second_problem_the_same_evening_rewrites_the_days_review(service_engine, user_id, web):
    # + 2023 Q III (35 marks, ok): 57/85 -> Good. Still one review, now no lapse.
    mysql = _topic(service_engine, user_id, web["course_id"], "MySQL")
    items = _items(service_engine, user_id, mysql, [5, 5, 15, 25])
    [q_iii] = _items(service_engine, user_id, mysql, [35])
    session = start_session(service_engine, user_id, "I3302", "practice", "topic", mysql)[
        "session_id"
    ]
    submit_attempts(service_engine, user_id, "I3302", _index_php(items), session)

    submit_attempts(
        service_engine, user_id, "I3302", [{"practice_item_id": q_iii, "correct": True}], session
    )

    state = _state(service_engine, mysql)
    assert (state.reps, state.lapses) == (1, 0)
    assert _count(service_engine, topic_state) == 1


def test_with_no_open_session_a_manual_practice_session_is_opened(service_engine, user_id, web):
    mysql = _topic(service_engine, user_id, web["course_id"], "MySQL")
    [item] = _items(service_engine, user_id, mysql, [5])

    result = submit_attempts(
        service_engine, user_id, "I3302", [{"practice_item_id": item, "correct": True}]
    )

    with service_engine.connect() as conn:
        row = conn.execute(select(study_session)).one()
    assert (row.id, row.mode, row.source) == (result["session_id"], "practice", "manual")


def test_an_open_session_is_used_when_none_is_named(service_engine, user_id, web):
    mysql = _topic(service_engine, user_id, web["course_id"], "MySQL")
    [item] = _items(service_engine, user_id, mysql, [5])
    session = start_session(service_engine, user_id, "I3302", "practice", "topic", mysql)

    result = submit_attempts(
        service_engine, user_id, "I3302", [{"practice_item_id": item, "correct": True}]
    )

    assert result["session_id"] == session["session_id"]
    assert _count(service_engine, study_session) == 1


def test_a_walkthrough_is_assisted_moves_no_state_and_records_its_position(
    service_engine, user_id, web
):
    mysql = _topic(service_engine, user_id, web["course_id"], "MySQL")
    paper = _paper(service_engine, user_id, web["slot_id"])
    items = _items(service_engine, user_id, mysql, [5, 5, 15, 25], paper=paper)
    session = start_session(
        service_engine, user_id, "I3302", "exam-walkthrough", "past-exam", paper
    )["session_id"]

    result = submit_attempts(service_engine, user_id, "I3302", _index_php(items)[:2], session)

    assert result["topics"] == [{"topic_id": mysql, "state": None}]
    assert _state(service_engine, mysql) is None
    with service_engine.connect() as conn:
        assert conn.execute(select(study_session.c.position)).scalar_one() == 2
        assert set(conn.execute(select(attempt.c.assisted)).scalars()) == {1}


def test_an_explanations_check_is_assisted(service_engine, user_id, web):
    # his pick: questions after an explanation are guided - the mistake log, not FSRS
    mysql = _topic(service_engine, user_id, web["course_id"], "MySQL")
    [item] = _items(service_engine, user_id, mysql, [5])
    session = start_session(service_engine, user_id, "I3302", "explain-chapter", "topic", mysql)

    submit_attempts(
        service_engine,
        user_id,
        "I3302",
        [{"practice_item_id": item, "correct": True}],
        session["session_id"],
    )

    with service_engine.connect() as conn:
        row = conn.execute(select(attempt.c.source, attempt.c.assisted)).one()
    assert (row.source, row.assisted) == ("comprehension-check", 1)
    assert _state(service_engine, mysql) is None


def test_a_mock_counts_as_unassisted_practice(service_engine, user_id, web):
    mysql = _topic(service_engine, user_id, web["course_id"], "MySQL")
    [item] = _items(service_engine, user_id, mysql, [5])
    session = start_session(service_engine, user_id, "I3302", "mock")

    submit_attempts(
        service_engine,
        user_id,
        "I3302",
        [{"practice_item_id": item, "correct": True}],
        session["session_id"],
    )

    assert _state(service_engine, mysql).reps == 1


def test_a_proposed_topic_is_not_practised(service_engine, user_id, web):
    # D-22: a proposed topic stays declinable only while it has no state.
    regex = _topic(service_engine, user_id, web["course_id"], "regex", status="proposed")

    with pytest.raises(StudyError) as err:
        submit_attempts(service_engine, user_id, "I3302", [{"topic_id": regex, "correct": True}])

    assert err.value.code == "topic_not_practisable"


def test_one_bad_part_saves_none_of_the_problem(service_engine, user_id, web):
    mysql = _topic(service_engine, user_id, web["course_id"], "MySQL")
    items = _items(service_engine, user_id, mysql, [5, 5])
    parts = [
        {"practice_item_id": items[0], "correct": True},
        {"practice_item_id": new_id(), "correct": True},  # no such item
    ]

    with pytest.raises(StudyError):
        submit_attempts(service_engine, user_id, "I3302", parts)

    assert _count(service_engine, attempt) == 0
    assert _count(service_engine, study_session) == 0
    assert _state(service_engine, mysql) is None


@pytest.mark.parametrize(
    ("part", "field"),
    [
        ({"correct": True}, "attempts[0].practice_item_id"),
        ({"practice_item_id": "x"}, "attempts[0].score"),
        ({"practice_item_id": "x", "score": 1.5}, "attempts[0].score"),
    ],
    ids=["no-question", "no-result", "score-above-1"],
)
def test_a_malformed_part_names_its_field(service_engine, user_id, web, part, field):
    with pytest.raises(StudyError) as err:
        submit_attempts(service_engine, user_id, "I3302", [part])
    assert err.value.field_errors[0]["field"] == field


# --- study_log_hours ----------------------------------------------------------------------


def test_logged_minutes_close_the_session(service_engine, user_id, web):
    mysql = _topic(service_engine, user_id, web["course_id"], "MySQL")
    session = start_session(service_engine, user_id, "I3302", "practice", "topic", mysql)

    result = log_hours(
        service_engine, user_id, "I3302", 40, topic_id=mysql, session_id=session["session_id"]
    )

    with service_engine.connect() as conn:
        entry = conn.execute(select(hours_entry)).one()
        ended = conn.execute(select(study_session.c.ended_at)).scalar_one()
    assert (entry.id, entry.minutes, entry.topic_id, entry.source) == (
        result["hours_entry_id"],
        40,
        mysql,
        "tool",
    )
    assert entry.occurred_at == session["started_at"]
    assert ended is not None


def test_hours_on_a_setup_item_hide_it_from_the_next_plan(service_engine, user_id, web):
    # Done when, second half: a plan raises first-material for I3302; 10 minutes against its
    # key and the next plan leaves it out (D-22's snooze, read since 1.15).
    first = get_plan(service_engine, user_id, TODAY)["lanes"]["setup"]["items"]
    key = next(i["setup_key"] for i in first if i["setup_kind"] == "first-material")

    log_hours(service_engine, user_id, "I3302", 10, setup_key=key)

    after = get_plan(service_engine, user_id, TODAY)["lanes"]["setup"]["items"]
    assert key not in {i["setup_key"] for i in after}


def test_a_setup_key_no_plan_raised_is_not_found(service_engine, user_id, web):
    with pytest.raises(StudyError) as err:
        log_hours(service_engine, user_id, "I3302", 10, setup_key="first-material:typo")
    assert err.value.code == "not_found"
    assert _count(service_engine, hours_entry) == 0


def test_a_topic_and_a_setup_key_together_are_refused(service_engine, user_id, web):
    mysql = _topic(service_engine, user_id, web["course_id"], "MySQL")
    with pytest.raises(StudyError) as err:
        log_hours(service_engine, user_id, "I3302", 10, topic_id=mysql, setup_key="k")
    assert err.value.field_errors[0]["field"] == "setup_key"


@pytest.mark.parametrize("minutes", [0, 721, 40.5, "forty"])
def test_minutes_are_whole_and_1_to_720(service_engine, user_id, web, minutes):
    with pytest.raises(StudyError) as err:
        log_hours(service_engine, user_id, "I3302", minutes)
    assert err.value.field_errors[0]["field"] == "minutes"


def test_a_mistyped_entry_is_voided_and_replaced_never_edited(service_engine, user_id, web):
    typo = log_hours(service_engine, user_id, "I3302", 400)["hours_entry_id"]

    fixed = log_hours(service_engine, user_id, "I3302", 40, voids=typo)

    with service_engine.connect() as conn:
        rows = {r.id: r for r in conn.execute(select(hours_entry))}
    assert rows[typo].minutes == 400 and rows[typo].voided_at is not None
    assert rows[fixed["hours_entry_id"]].voided_at is None
    assert fixed["voided"] == typo


def test_voiding_alone_writes_no_new_entry(service_engine, user_id, web):
    twice = log_hours(service_engine, user_id, "I3302", 40)["hours_entry_id"]

    result = log_hours(service_engine, user_id, "I3302", voids=twice)

    assert result == {"hours_entry_id": None, "minutes": None, "voided": twice}
    assert _count(service_engine, hours_entry) == 1


def test_an_entry_already_voided_cannot_be_voided_again(service_engine, user_id, web):
    entry = log_hours(service_engine, user_id, "I3302", 40)["hours_entry_id"]
    log_hours(service_engine, user_id, "I3302", voids=entry)

    with pytest.raises(StudyError) as err:
        log_hours(service_engine, user_id, "I3302", voids=entry)
    assert err.value.code == "not_found"


# --- the three tools ------------------------------------------------------------------------


def _body(result):
    return json.loads(result.content[0].text)


def test_the_three_loop_tools_are_listed(service_engine):
    async def go():
        async with Client(create_server(service_engine)) as client:
            return await client.list_tools()

    names = {t.name for t in asyncio.run(go()).tools}
    assert {"study_start_session", "study_submit_attempt", "study_log_hours"} <= names


def test_the_mysql_evening_through_the_tools(call, service_engine, user_id, web):
    mysql = _topic(service_engine, user_id, web["course_id"], "MySQL")
    items = _items(service_engine, user_id, mysql, [5, 5, 15, 25])

    started = call(
        "study_start_session",
        {"code": "I3302", "mode": "practice", "subject_type": "topic", "subject_id": mysql},
    )
    session = _body(started)["session_id"]
    graded = call(
        "study_submit_attempt",
        {"code": "I3302", "attempts": _index_php(items), "session_id": session},
    )
    logged = call(
        "study_log_hours",
        {"code": "I3302", "minutes": "40", "topic_id": mysql, "session_id": session},
    )

    assert not (started.is_error or graded.is_error or logged.is_error)
    assert _body(graded)["topics"][0]["reps"] == 1
    assert _body(logged)["minutes"] == 40
    assert _state(service_engine, mysql) is not None


def test_a_bad_value_comes_back_in_the_error_shape(call, web):
    result = call("study_log_hours", {"code": "I3302", "minutes": 0})

    assert result.is_error
    assert _body(result)["field_errors"][0]["field"] == "minutes"


# --- after QA (D-112) -------------------------------------------------------------------------


def test_a_submit_logs_each_topics_day_score_and_rating(service_engine, user_id, web, caplog):
    # "why is MySQL due tomorrow?" - the rating is stored nowhere, so the log line carries it.
    mysql = _topic(service_engine, user_id, web["course_id"], "MySQL")
    items = _items(service_engine, user_id, mysql, [5, 5, 15, 25])

    with caplog.at_level(logging.INFO, logger="studysystem.services.study_loop"):
        result = submit_attempts(service_engine, user_id, "I3302", _index_php(items))

    [line] = [json.loads(r.message) for r in caplog.records if "submit_attempt" in r.message]
    assert line["session_id"] == result["session_id"]
    assert (line["mode"], line["attempts"]) == ("practice", 4)
    [logged] = line["topics"]
    assert (logged["day_score"], logged["rating"], logged["lapses"]) == (0.44, "Again", 1)
    assert isinstance(line["ms"], int)


def test_an_item_of_another_course_names_the_item_field(service_engine, user_id, web):
    other = add_course(service_engine, user_id, "I3350", "Mobile", SEM)
    android = _topic(service_engine, user_id, other["course_id"], "Android")
    [theirs] = _items(service_engine, user_id, android, [5])

    with pytest.raises(StudyError) as err:
        submit_attempts(
            service_engine, user_id, "I3302", [{"practice_item_id": theirs, "correct": True}]
        )

    assert err.value.field_errors[0]["field"] == "attempts[0].practice_item_id"


def test_voiding_alone_refuses_a_scope_it_would_drop(service_engine, user_id, web):
    entry = log_hours(service_engine, user_id, "I3302", 40)["hours_entry_id"]
    session = start_session(service_engine, user_id, "I3302", "practice")["session_id"]

    with pytest.raises(StudyError) as err:
        log_hours(service_engine, user_id, "I3302", voids=entry, session_id=session)

    assert err.value.field_errors[0]["field"] == "session_id"
    with service_engine.connect() as conn:
        assert conn.execute(select(hours_entry.c.voided_at)).scalar_one() is None


def test_attempts_still_land_in_an_expired_session(service_engine, user_id, web):
    # His pick: "records nothing" is about minutes - an attempt carries its own time.
    mysql = _topic(service_engine, user_id, web["course_id"], "MySQL")
    [item] = _items(service_engine, user_id, mysql, [5])
    session = start_session(service_engine, user_id, "I3302", "practice")["session_id"]
    with write_unit(service_engine) as conn:
        conn.execute(
            update(study_session)
            .where(study_session.c.id == session)
            .values(started_at="2026-10-01T16:00:00Z", expires_at="2026-10-01T20:00:00Z")
        )

    result = submit_attempts(
        service_engine, user_id, "I3302", [{"practice_item_id": item, "correct": True}], session
    )

    assert result["session_id"] == session


def test_an_unknown_mark_borrows_from_every_known_mark_rejected_items_too(
    service_engine, user_id, web
):
    # Items 5 and 5 live, 20 rejected -> average 10. An unknown part (ok) + a 5-mark part
    # (wrong) -> 10 / 15 = 0.67 -> Good. Live items only would give 5 / 10 = 0.5 -> Again.
    mysql = _topic(service_engine, user_id, web["course_id"], "MySQL")
    known, _, rejected = _items(service_engine, user_id, mysql, [5, 5, 20])
    [unknown] = _items(service_engine, user_id, mysql, [None])
    with write_unit(service_engine) as conn:
        conn.execute(
            update(practice_item).where(practice_item.c.id == rejected).values(state="rejected")
        )

    parts = [
        {"practice_item_id": unknown, "correct": True},
        {"practice_item_id": known, "correct": False},
    ]
    submit_attempts(service_engine, user_id, "I3302", parts)

    assert _state(service_engine, mysql).lapses == 0  # Good
