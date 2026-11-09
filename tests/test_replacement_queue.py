"""The replacement queue (4.3; D-16 d and f, D-259): a pending row on a topic is filled by a
practice task scoped to that topic, written from the material its hole came from, with the
rejected item marked "do not repeat this". Split and decline drop a pending row, merge moves it.

Most tests queue their rows directly, so they hold whatever rule decides which rejections queue
(`_queue_rejection`); the last one runs the whole path through `study_submit_feedback`.
"""

# ruff: noqa: F811 - the fixtures are imported, then named as test parameters

import json

import pytest
from sqlalchemy import func, insert, select, update

from studysystem.db.tables import (
    generation_task,
    item_rejection,
    practice_item,
    replacement_queue,
    topic,
)
from studysystem.errors import StudyError
from studysystem.services.feedback import submit_feedback
from studysystem.services.generation import start_generation, submit_generation
from studysystem.services.ids import new_id, now
from studysystem.services.taxonomy import merge_topics, split_topic
from studysystem.services.topics import confirm_topic_proposal
from studysystem.services.units import write_unit
from tests._papers import pdf_pages
from tests.test_note_generation import ch6, i3302, material, web  # noqa: F401
from tests.test_practice_generation import generated, item, no_key, submit, topic_ids


def practice(engine, user_id, material_id, topic_id, n=3):
    """A closed practice task on `material_id`: `n` items, all on `topic_id`. Returns the item
    ids in order."""
    task = start_generation(engine, user_id, "practice", material_id=material_id, count=n)
    submit(engine, user_id, task, [item(topic_id, k) for k in range(1, n + 1)])
    with engine.connect() as conn:
        return (
            conn.execute(
                select(practice_item.c.id)
                .where(practice_item.c.task_id == task.payload["task_id"])
                .order_by(practice_item.c.item_ordinal)
            )
            .scalars()
            .all()
        )


def reject(engine, user_id, item_id, reason="unclear", note=None) -> str:
    """What a queued rejection leaves, written directly: the rejection, the item retired, the
    pending row on its topic. Returns the row's id."""
    row_id, rejection_id = new_id(), new_id()
    with write_unit(engine) as conn:
        topic_id = conn.execute(
            select(practice_item.c.topic_id).where(practice_item.c.id == item_id)
        ).scalar_one()
        conn.execute(
            insert(item_rejection).values(
                id=rejection_id,
                user_id=user_id,
                practice_item_id=item_id,
                reason=reason,
                note=note,
                created_at=now(),
            )
        )
        conn.execute(
            update(practice_item).where(practice_item.c.id == item_id).values(state="rejected")
        )
        conn.execute(
            insert(replacement_queue).values(
                id=row_id,
                user_id=user_id,
                topic_id=topic_id,
                rejection_id=rejection_id,
                created_at=now(),
            )
        )
    return row_id


def replace(engine, user_id, topic_id, **kwargs):
    return start_generation(engine, user_id, "practice", topic_id=topic_id, **kwargs)


def rows(engine):
    with engine.connect() as conn:
        return conn.execute(select(replacement_queue).order_by(replacement_queue.c.id)).all()


def tagging(engine, user_id, material_id):
    """A tagging task on the material - the same pages and active topics a practice task
    sends, and no practice task left open to collide with the ones a test starts."""
    return start_generation(engine, user_id, "tagging", material_id=material_id)


def pages_of(engine, user_id, material_id):
    """The pages a task on `material_id` sends - what a replacement should read."""
    return tagging(engine, user_id, material_id).payload["content"]


def other_topic(engine, user_id, material_id, topic_id):
    return next(t for t in topic_ids(tagging(engine, user_id, material_id)) if t != topic_id)


@pytest.fixture
def topic_a(service_engine, user_id, ch6):
    """The first active topic of I3302 that a task on Chapter 6 lists."""
    return topic_ids(tagging(service_engine, user_id, ch6))[0]


# --- [4] the start ------------------------------------------------------------------------


def test_the_start_sends_the_source_chapter_the_topic_and_the_rejected_item(
    service_engine, user_id, ch6, topic_a
):
    q1, _, _ = practice(service_engine, user_id, ch6, topic_a)
    reject(service_engine, user_id, q1, reason="off-syllabus", note="QUIC is not taught")

    task = replace(service_engine, user_id, topic_a)

    rules = task.payload["rules"]
    assert [t["id"] for t in rules["topics"]] == [topic_a]
    assert rules["topics"][0]["past_exam_questions"]  # the topic's real questions travel
    assert rules["count"] == 1
    (repeat,) = rules["do_not_repeat"]
    assert repeat["reason"] == "off-syllabus" and repeat["note"] == "QUIC is not taught"
    assert repeat["question"].startswith("Practice question 1")
    assert task.payload["content"] == pages_of(service_engine, user_id, ch6)
    with service_engine.connect() as conn:
        row = conn.execute(
            select(generation_task).where(generation_task.c.id == task.payload["task_id"])
        ).one()
    assert (row.kind, row.scope_type, row.scope_id, row.item_count) == (
        "practice",
        "topic",
        topic_a,
        1,
    )


def test_a_topic_with_nothing_queued_or_retired_is_refused(
    service_engine, user_id, other_user_id, ch6, topic_a
):
    with pytest.raises(StudyError) as nothing:
        replace(service_engine, user_id, topic_a)
    assert nothing.value.code == "nothing_queued"

    q1, _, _ = practice(service_engine, user_id, ch6, topic_a)
    reject(service_engine, user_id, q1)
    with pytest.raises(StudyError) as theirs:
        replace(service_engine, other_user_id, topic_a)
    assert theirs.value.code == "not_found"

    with write_unit(service_engine) as conn:
        conn.execute(update(topic).where(topic.c.id == topic_a).values(status="superseded"))
    with pytest.raises(StudyError) as retired:
        replace(service_engine, user_id, topic_a)
    assert retired.value.code == "not_found"


@pytest.mark.parametrize("extra", [{"material_id": "x" * 26}, {"count": 2}])
def test_a_topic_task_takes_no_material_or_count(service_engine, user_id, topic_a, extra):
    with pytest.raises(StudyError) as refused:
        replace(service_engine, user_id, topic_a, **extra)

    assert (refused.value.code, refused.value.field_errors[0]["field"]) == (
        "invalid_value",
        "topic_id",
    )


def test_count_is_the_rows_from_the_oldest_rows_material(
    service_engine, user_id, ch6, topic_a, tmp_path
):
    """Chapter 6's item, then Chapter 7's, then Chapter 6's again: the task reads Chapter 6 and
    writes 2 - Chapter 7's row waits for its own task."""
    ch7 = material(
        service_engine,
        user_id,
        tmp_path / "Course Notes",
        name="PHP_Chapter7_Sessions.pdf",
        data=pdf_pages(["PHP sessions", "session_start and $_SESSION"]),
    )
    six = practice(service_engine, user_id, ch6, topic_a, n=2)
    (seven,) = practice(service_engine, user_id, ch7, topic_a, n=1)
    reject(service_engine, user_id, six[0])
    reject(service_engine, user_id, seven)
    reject(service_engine, user_id, six[1])

    task = replace(service_engine, user_id, topic_a)

    assert task.payload["rules"]["count"] == 2
    assert len(task.payload["rules"]["do_not_repeat"]) == 3  # every rejected item waiting
    assert task.payload["content"] == pages_of(service_engine, user_id, ch6)


def test_the_open_task_is_sent_again(service_engine, user_id, ch6, topic_a):
    q1, _, _ = practice(service_engine, user_id, ch6, topic_a)
    reject(service_engine, user_id, q1)

    first = replace(service_engine, user_id, topic_a)
    again = replace(service_engine, user_id, topic_a)

    assert again.payload["task_id"] == first.payload["task_id"]
    with service_engine.connect() as conn:
        n = conn.execute(
            select(func.count())
            .select_from(generation_task)
            .where(generation_task.c.scope_type == "topic")
        ).scalar_one()
    assert n == 1


# --- [5] the fill -------------------------------------------------------------------------


def test_each_kept_item_fills_the_oldest_row(service_engine, user_id, ch6, topic_a):
    q1, q2, _ = practice(service_engine, user_id, ch6, topic_a)
    older = reject(service_engine, user_id, q1)
    newer = reject(service_engine, user_id, q2)
    task = replace(service_engine, user_id, topic_a)

    first = submit(service_engine, user_id, task, [item(topic_a, 1), no_key(topic_a, 2)])

    assert first["accepted"] == [1]
    by_id = {r.id: r for r in rows(service_engine)}
    assert (by_id[older].status, by_id[older].filled_by_task_id) == (
        "filled",
        task.payload["task_id"],
    )
    assert by_id[older].resolved_at is not None
    assert by_id[newer].status == "pending"

    second = submit(service_engine, user_id, task, [item(topic_a, 2)])

    assert second["status"] == "closed"
    assert {r.status for r in rows(service_engine)} == {"filled"}


def test_an_item_on_another_topic_is_refused_in_a_topic_task(service_engine, user_id, ch6, topic_a):
    q1, _, _ = practice(service_engine, user_id, ch6, topic_a)
    reject(service_engine, user_id, q1)
    task = replace(service_engine, user_id, topic_a)
    other = other_topic(service_engine, user_id, ch6, topic_a)

    reply = submit(service_engine, user_id, task, [item(other, 1)])

    (error,) = reply["item_errors"]
    assert (error["item_ordinal"], error["code"], error["field"]) == (1, "wrong_topic", "topic_id")
    assert [r.status for r in rows(service_engine)] == ["pending"]


def test_items_past_the_count_are_dropped(service_engine, user_id, ch6, topic_a):
    q1, _, _ = practice(service_engine, user_id, ch6, topic_a)
    reject(service_engine, user_id, q1)
    task = replace(service_engine, user_id, topic_a)

    reply = submit(service_engine, user_id, task, [item(topic_a, 1), item(topic_a, 2)])

    assert reply["accepted"] == [1]
    assert [r.status for r in rows(service_engine)] == ["filled"]


def test_a_topic_task_that_ends_partial_queues_nothing_new(service_engine, user_id, ch6, topic_a):
    """[2] case 9: its unfilled row is already queued - a second row would double the hole."""
    q1, _, _ = practice(service_engine, user_id, ch6, topic_a)
    reject(service_engine, user_id, q1)
    task = replace(service_engine, user_id, topic_a)

    replies = [submit(service_engine, user_id, task, [no_key(topic_a, 1)]) for _ in range(3)]

    assert [r["status"] for r in replies] == ["open", "open", "partial"]
    assert [r.status for r in rows(service_engine)] == ["pending"]


# --- [3] the chain ------------------------------------------------------------------------


def test_a_replacements_replacement_reads_the_same_chapter(service_engine, user_id, ch6, topic_a):
    q1, _, _ = practice(service_engine, user_id, ch6, topic_a)
    reject(service_engine, user_id, q1)
    first = replace(service_engine, user_id, topic_a)
    submit(service_engine, user_id, first, [item(topic_a, 7)])
    with service_engine.connect() as conn:
        replacement = conn.execute(
            select(practice_item.c.id).where(practice_item.c.task_id == first.payload["task_id"])
        ).scalar_one()
    reject(service_engine, user_id, replacement)

    second = replace(service_engine, user_id, topic_a)

    assert second.payload["task_id"] != first.payload["task_id"]
    assert second.payload["content"] == pages_of(service_engine, user_id, ch6)


def test_a_chain_with_no_material_sends_no_pages(service_engine, user_id, topic_a):
    """An item from a topic task that filled no row (made before 4.3): no material to read, so
    the host writes from the topic's past-exam questions."""
    task_id, item_id = new_id(), new_id()
    with write_unit(service_engine) as conn:
        conn.execute(
            insert(generation_task).values(
                id=task_id,
                user_id=user_id,
                kind="practice",
                scope_type="topic",
                scope_id=topic_a,
                route="host",
                status="closed",
                closed_at=now(),
                created_at=now(),
            )
        )
        conn.execute(
            insert(practice_item).values(
                id=item_id,
                user_id=user_id,
                topic_id=topic_a,
                task_id=task_id,
                item_ordinal=1,
                question="Write a prepared statement that ...",
                answer_key="$stmt = $db->prepare(...)",
                answer_provenance="unverified",
                novelty_score=0.2,
                origin="generated",
                source_marker="model-knowledge",
                created_at=now(),
            )
        )
    reject(service_engine, user_id, item_id)

    task = replace(service_engine, user_id, topic_a)

    assert task.payload["content"] == []
    assert task.payload["parts"] == 1


# --- [6] split, merge, decline ------------------------------------------------------------


def listed_items(engine, topic_id) -> list[str]:
    with engine.connect() as conn:
        return (
            conn.execute(
                select(practice_item.c.id)
                .where(
                    practice_item.c.topic_id == topic_id,
                    practice_item.c.state.in_(("live", "needs-review")),
                )
                .order_by(practice_item.c.id)
            )
            .scalars()
            .all()
        )


def test_a_split_drops_the_parents_pending_rows(service_engine, user_id, ch6, topic_a):
    q1, q2, _ = practice(service_engine, user_id, ch6, topic_a)
    filled_row = reject(service_engine, user_id, q1)
    task = replace(service_engine, user_id, topic_a)
    submit(service_engine, user_id, task, [item(topic_a, 1)])
    pending_row = reject(service_engine, user_id, q2)
    listed = listed_items(service_engine, topic_a)

    split_topic(
        service_engine,
        user_id,
        topic_a,
        [
            {"name": "Half one", "item_ids": listed[:1]},
            {"name": "Half two", "item_ids": listed[1:]},
        ],
    )

    by_id = {r.id: r for r in rows(service_engine)}
    assert (by_id[pending_row].status, by_id[pending_row].topic_id) == ("dropped", topic_a)
    assert by_id[pending_row].resolved_at is not None
    assert by_id[filled_row].status == "filled"  # a filled row is history, never touched


def test_a_merge_moves_the_sources_pending_rows(service_engine, user_id, ch6, topic_a):
    q1, _, _ = practice(service_engine, user_id, ch6, topic_a)
    row_id = reject(service_engine, user_id, q1)
    target = other_topic(service_engine, user_id, ch6, topic_a)

    merge_topics(service_engine, user_id, topic_a, target)

    (row,) = rows(service_engine)
    assert (row.id, row.status, row.topic_id) == (row_id, "pending", target)
    assert replace(service_engine, user_id, target).payload["rules"]["count"] == 1


def test_a_decline_drops_the_topics_pending_rows(service_engine, user_id, ch6, topic_a):
    q1, _, _ = practice(service_engine, user_id, ch6, topic_a)
    row_id = reject(service_engine, user_id, q1)
    target = other_topic(service_engine, user_id, ch6, topic_a)
    with write_unit(service_engine) as conn:
        conn.execute(update(topic).where(topic.c.id == topic_a).values(status="proposed"))

    confirm_topic_proposal(
        service_engine,
        user_id,
        topic_a,
        "decline",
        [{"item_id": i, "topic": {"id": target}} for i in listed_items(service_engine, topic_a)],
    )

    (row,) = rows(service_engine)
    assert (row.id, row.status) == (row_id, "dropped")


# --- Done when: a rejected item is replaced without regenerating the set around it -------


def test_a_rejected_item_is_replaced_and_the_set_around_it_stays(
    service_engine, user_id, ch6, topic_a, call
):
    """I3302 Chapter 6's set of 5; he rejects item 3 as unclear. One topic task writes one
    item; the other four are the same rows, untouched, and the set's task stays closed."""
    task = start_generation(service_engine, user_id, "practice", material_id=ch6, count=5)
    topics = topic_ids(task)
    submit(service_engine, user_id, task, [item(topics[k % len(topics)], k + 1) for k in range(5)])
    before = generated(service_engine)
    third = before[2]

    filed = submit_feedback(
        service_engine, user_id, kind="item-defect", practice_item_id=third.id, reason="unclear"
    )
    assert filed["queued"]["topic_id"] == third.topic_id

    started = call(
        "study_start_generation", {"kind": "practice", "topic_id": filed["queued"]["topic_id"]}
    )
    assert not started.is_error
    replacement_task = json.loads(started.content[0].text)
    assert replacement_task["rules"]["count"] == 1
    reply = submit_generation(
        service_engine,
        user_id,
        replacement_task["task_id"],
        {"items": [item(third.topic_id, 6)]},
    )

    assert (reply["status"], reply["accepted"]) == ("closed", [1])
    (row,) = rows(service_engine)
    assert (row.status, row.filled_by_task_id) == ("filled", replacement_task["task_id"])
    after = {r.id: r for r in generated(service_engine)}
    assert len(after) == 6
    for old in before:
        if old.id != third.id:
            assert after[old.id] == old  # every column as it was
    assert after[third.id].state == "rejected"
    with service_engine.connect() as conn:
        set_status = conn.execute(
            select(generation_task.c.status).where(generation_task.c.id == task.payload["task_id"])
        ).scalar_one()
    assert set_status == "closed"
