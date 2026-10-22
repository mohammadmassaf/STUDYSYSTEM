"""The practice task (2.7; D-47, D-11, D-16, D-22, D-24, D-211): the host writes new questions from
one material, each with a topic and an answer key. The server checks each item alone and keeps the
valid ones. A generated key is always `unverified`; a high novelty score is stored, never a gate;
only the topic gate parks an item. Written first - these fail until `kind="practice"` is built."""

# ruff: noqa: F811 - the fixtures are imported, then named as test parameters

import pytest
from sqlalchemy import insert, select, update

from studysystem.db.tables import course, practice_item, topic
from studysystem.errors import StudyError
from studysystem.services.courses import add_course
from studysystem.services.generation import start_generation, submit_generation
from studysystem.services.ids import new_id, now
from studysystem.services.units import write_unit
from tests.test_note_generation import ch6, i3302, web  # noqa: F401
from tests.test_profiles import SEM


def start(engine, user_id, material_id, **kwargs):
    return start_generation(engine, user_id, "practice", material_id=material_id, **kwargs)


def item(topic_id, n=1, **fields) -> dict:
    return {
        "question": f"Practice question {n}: explain how the topic's main idea works",
        "answer_key": f"Answer {n}",
        "topic_id": topic_id,
        "source": "from-material",
        **fields,
    }


def submit(engine, user_id, task, items) -> dict:
    return submit_generation(engine, user_id, task.payload["task_id"], {"items": items})


def generated(engine):
    with engine.connect() as conn:
        return conn.execute(
            select(practice_item)
            .where(practice_item.c.origin == "generated")
            .order_by(practice_item.c.item_ordinal)
        ).all()


def topic_ids(task) -> list[str]:
    return [t["id"] for t in task.payload["rules"]["topics"]]


def set_topic_status(engine, topic_id, status) -> None:
    with write_unit(engine) as conn:
        conn.execute(update(topic).where(topic.c.id == topic_id).values(status=status))


# --- the core: the happy path, the key rule, the gate ----------------------------------


def test_three_good_items_land_live_unverified_with_their_keys_and_a_score(
    service_engine, user_id, ch6
):
    task = start(service_engine, user_id, ch6)
    topics = topic_ids(task)

    reply = submit(
        service_engine, user_id, task, [item(topics[0], 1), item(topics[1], 2), item(topics[2], 3)]
    )

    assert (reply["status"], reply["accepted"], reply["item_errors"]) == ("closed", [1, 2, 3], [])
    rows = generated(service_engine)
    assert [r.topic_id for r in rows] == topics[:3]
    for n, row in enumerate(rows, start=1):
        assert (row.state, row.origin, row.answer_provenance) == ("live", "generated", "unverified")
        assert (row.answer_key, row.source_marker) == (f"Answer {n}", "from-material")
        assert (row.past_exam_id, row.position) == (None, None)
        assert row.novelty_score is not None and 0 <= row.novelty_score <= 1


def test_an_item_with_no_key_is_rejected_alone_and_the_others_are_kept(
    service_engine, user_id, ch6
):
    task = start(service_engine, user_id, ch6)
    topics = topic_ids(task)
    no_key = item(topics[1], 2)
    del no_key["answer_key"]

    reply = submit(service_engine, user_id, task, [item(topics[0], 1), no_key, item(topics[2], 3)])

    assert reply["accepted"] == [1, 3]  # places in this payload
    (error,) = reply["item_errors"]
    assert (error["item_ordinal"], error["field"]) == (2, "answer_key")
    # items are numbered as they land, so a retry's one fixed item does not collide
    assert [r.item_ordinal for r in generated(service_engine)] == [1, 2]
    assert reply["status"] == "open"  # an error leaves the task open for a retry (D-11)


def test_an_item_on_a_proposed_topic_is_parked_for_review(service_engine, user_id, ch6):
    task = start(service_engine, user_id, ch6)
    topics = topic_ids(task)
    set_topic_status(service_engine, topics[1], "proposed")

    reply = submit(service_engine, user_id, task, [item(topics[0], 1), item(topics[1], 2)])

    assert reply["accepted"] == [1, 2]  # parked is accepted, not rejected (D-22)
    assert [r.state for r in generated(service_engine)] == ["live", "needs-review"]


# --- the rest -----------------------------------------------------------------------------


def test_an_item_on_another_courses_topic_is_rejected_alone(service_engine, user_id, ch6):
    task = start(service_engine, user_id, ch6)
    add_course(service_engine, user_id, "I3304", "Computer Networks", SEM)
    with write_unit(service_engine) as conn:
        other_course = conn.execute(
            select(course.c.id).where(course.c.code == "I3304")
        ).scalar_one()
        foreign = new_id()
        conn.execute(
            insert(topic).values(
                id=foreign,
                user_id=user_id,
                course_id=other_course,
                name="OSI model",
                status="active",
                proposed_by="material",
                status_changed_at=now(),
                created_at=now(),
            )
        )

    reply = submit(service_engine, user_id, task, [item(topic_ids(task)[0], 1), item(foreign, 2)])

    assert reply["accepted"] == [1]
    (error,) = reply["item_errors"]
    assert (error["item_ordinal"], error["field"]) == (2, "topic_id")
    assert [r.item_ordinal for r in generated(service_engine)] == [1]


def test_count_keeps_the_first_n_items_and_drops_the_rest(service_engine, user_id, ch6):
    task = start(service_engine, user_id, ch6, count=2)
    topics = topic_ids(task)

    reply = submit(
        service_engine, user_id, task, [item(topics[0], 1), item(topics[1], 2), item(topics[2], 3)]
    )

    assert reply["accepted"] == [1, 2]
    assert reply["item_errors"] == []  # a dropped extra is not an error
    assert [r.item_ordinal for r in generated(service_engine)] == [1, 2]


def test_a_copy_of_a_past_exam_question_stays_live_with_a_high_score(service_engine, user_id, ch6):
    task = start(service_engine, user_id, ch6)
    with service_engine.connect() as conn:
        past = conn.execute(
            select(practice_item.c.question)
            .where(practice_item.c.origin == "past-exam")
            .order_by(practice_item.c.id)
            .limit(1)
        ).scalar_one()
    topics = topic_ids(task)

    submit(
        service_engine,
        user_id,
        task,
        [item(topics[0], 1, question=past), item(topics[1], 2)],
    )

    copy, fresh = generated(service_engine)
    assert copy.state == "live"  # D-211: the score is stored, never a gate
    assert copy.novelty_score >= 0.9
    assert fresh.novelty_score < copy.novelty_score


def test_a_host_sent_provenance_field_is_rejected_for_that_item(service_engine, user_id, ch6):
    task = start(service_engine, user_id, ch6)
    topics = topic_ids(task)

    reply = submit(
        service_engine,
        user_id,
        task,
        [item(topics[0], 1), item(topics[1], 2, answer_provenance="verified-source")],
    )

    assert reply["accepted"] == [1]
    (error,) = reply["item_errors"]
    assert (error["item_ordinal"], error["field"]) == (2, "answer_provenance")
    assert [r.answer_provenance for r in generated(service_engine)] == ["unverified"]


def test_the_start_reply_carries_the_courses_active_topics_with_ids(service_engine, user_id, ch6):
    task = start(service_engine, user_id, ch6)

    topics = task.payload["rules"]["topics"]
    assert len(topics) == 6 and all(len(t["id"]) == 26 and t["name"] for t in topics)
    assert task.payload["content"] and task.payload["schema"]


def test_another_students_material_is_not_found(service_engine, user_id, other_user_id, ch6):
    with pytest.raises(StudyError) as excinfo:
        start(service_engine, other_user_id, ch6)

    assert excinfo.value.code == "not_found"


def test_count_is_the_tasks_total_across_resubmits(service_engine, user_id, ch6):
    task = start(service_engine, user_id, ch6, count=2)
    topics = topic_ids(task)
    no_key = item(topics[1], 2)
    del no_key["answer_key"]

    first = submit(service_engine, user_id, task, [item(topics[0], 1), no_key])
    second = submit(
        service_engine, user_id, task, [item(topics[1], 2), item(topics[2], 3), item(topics[0], 4)]
    )

    assert first["accepted"] == [1] and first["status"] == "open"
    assert second["accepted"] == [1]  # one slot left of the 2 asked for
    assert len(generated(service_engine)) == 2


@pytest.mark.parametrize("bad", [0, 2.5, -1, "many"])
def test_a_count_that_is_not_a_whole_number_from_1_is_refused(service_engine, user_id, ch6, bad):
    with pytest.raises(StudyError) as excinfo:
        start(service_engine, user_id, ch6, count=bad)

    assert (excinfo.value.code, excinfo.value.field_errors[0]["field"]) == (
        "invalid_value",
        "count",
    )
