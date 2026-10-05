"""The tagging task (1.15b; D-93, D-102, D-104): Chapter 6 leaves with I3302's `active` topics,
each defined by its past-exam questions, and comes back as the ids it teaches. Each id is judged
alone - an `active` topic of this course, once per payload - and becomes an `inferred`
topic -> material edge unless that exact pair is already linked. A material can be tagged again;
a course with no active topic is refused."""

import json

import pytest
from sqlalchemy import func, insert, select

from studysystem.db.tables import coverage, generation_task, topic
from studysystem.errors import StudyError
from studysystem.services.courses import add_course
from studysystem.services.generation import (
    TAGGING_SCHEMA,
    TAGGING_STEPS,
    start_generation,
    submit_generation,
)
from studysystem.services.ids import new_id, now
from studysystem.services.units import write_unit
from tests.test_note_generation import ch6, i3302, material, web  # noqa: F401 - fixtures
from tests.test_profiles import NAMES, SEM


def start(engine, user_id, material_id):
    return start_generation(engine, user_id, "tagging", material_id=material_id)


def submit(engine, user_id, task_id, *topic_ids):
    return submit_generation(engine, user_id, task_id, {"topics": [{"id": t} for t in topic_ids]})


def topic_ids(engine) -> dict[str, str]:
    """{name: id} of every topic, any status."""
    with engine.connect() as conn:
        return dict(conn.execute(select(topic.c.name, topic.c.id)).tuples().all())


def add_topic(engine, user_id, course_id, name, status) -> str:
    """A topic straight into the table - a declined one, or one of another course."""
    topic_id = new_id()
    with write_unit(engine) as conn:
        conn.execute(
            insert(topic).values(
                id=topic_id,
                user_id=user_id,
                course_id=course_id,
                name=name,
                status=status,
                proposed_by="profile",
                status_changed_at=now(),
                created_at=now(),
            )
        )
    return topic_id


def chapter_edges(engine, material_id) -> list[str]:
    """The topic ids linked to `material_id`, one entry per edge row."""
    with engine.connect() as conn:
        return sorted(
            conn.execute(
                select(coverage.c.topic_id).where(coverage.c.material_id == material_id)
            ).scalars()
        )


def task_row(engine, task_id):
    with engine.connect() as conn:
        return conn.execute(select(generation_task).where(generation_task.c.id == task_id)).one()


def tagging_tasks(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(
            select(func.count())
            .select_from(generation_task)
            .where(generation_task.c.kind == "tagging")
        ).scalar_one()


@pytest.fixture
def ids(service_engine, i3302):  # noqa: F811
    return topic_ids(service_engine)


@pytest.fixture
def task_id(service_engine, user_id, ch6):  # noqa: F811
    return start(service_engine, user_id, ch6).payload["task_id"]


# --- start ----------------------------------------------------------------------------------


def test_chapter_6_leaves_with_every_active_topic_and_its_questions(
    service_engine,
    user_id,
    i3302,  # noqa: F811
    ch6,  # noqa: F811
    ids,
):
    declined = add_topic(service_engine, user_id, i3302["course_id"], "Web security", "declined")

    payload = start(service_engine, user_id, ch6).payload

    assert payload["schema"] == TAGGING_SCHEMA
    assert payload["rules"]["steps"] == TAGGING_STEPS
    sent = {t["name"]: t for t in payload["rules"]["topics"]}
    assert set(sent) == set(NAMES.values())  # the six accepted; the declined one stays home
    assert declined not in {t["id"] for t in payload["rules"]["topics"]}
    for entry in sent.values():
        assert entry["past_exam_questions"]  # each topic travels with its questions (D-104)
        assert "weight" not in entry  # tagging is a yes or no, not a ranking
    first = sent[NAMES["mysql"]]["past_exam_questions"][0]
    assert set(first) == {"paper", "position", "question"}
    assert first["question"].startswith("Question ")


def test_one_task_row_with_no_profile(service_engine, user_id, ch6, task_id):  # noqa: F811
    row = task_row(service_engine, task_id)
    assert (row.kind, row.scope_type, row.scope_id) == ("tagging", "material", ch6)
    assert row.exam_profile_id is None and row.status == "open" and row.bytes_sent > 0


def test_starting_again_hands_back_the_same_open_task(service_engine, user_id, ch6, task_id):  # noqa: F811
    first = task_row(service_engine, task_id).bytes_sent

    again = start(service_engine, user_id, ch6).payload["task_id"]

    assert again == task_id and tagging_tasks(service_engine) == 1
    assert task_row(service_engine, task_id).bytes_sent == 2 * first  # D-46: the resend counts


def test_a_tagged_material_can_be_tagged_again(service_engine, user_id, ch6, task_id, ids):  # noqa: F811
    submit(service_engine, user_id, task_id, ids[NAMES["mysql"]])  # closes it

    again = start(service_engine, user_id, ch6).payload["task_id"]

    assert again != task_id and tagging_tasks(service_engine) == 2  # C1: no "already tagged"


def test_a_course_with_no_active_topic_is_refused(service_engine, user_id, tmp_path):
    # I3301 today: chapter 0.pdf, no paper transcribed. A proposal is not active either.
    course = add_course(service_engine, user_id, "I3302", "Server-Side Web Development", SEM)
    add_topic(service_engine, user_id, course["course_id"], "PHP forms", "proposed")
    chapter = material(service_engine, user_id, tmp_path)

    with pytest.raises(StudyError) as err:
        start(service_engine, user_id, chapter)

    assert err.value.code == "no_active_topics"
    assert err.value.field_errors[0]["field"] == "material_id"
    assert tagging_tasks(service_engine) == 0


def test_a_tagging_needs_a_material(service_engine, user_id, web):  # noqa: F811
    with pytest.raises(StudyError) as err:
        start(service_engine, user_id, None)
    assert err.value.field_errors[0]["field"] == "material_id"


def test_each_start_logs_one_json_line(service_engine, user_id, ch6, caplog):  # noqa: F811
    with caplog.at_level("INFO", logger="studysystem.services.generation"):
        start(service_engine, user_id, ch6)

    line = json.loads(caplog.records[-1].getMessage())
    assert (line["event"], line["kind"], line["topics"]) == ("start_generation", "tagging", 6)
    assert line["questions"] > 0


# --- submit ---------------------------------------------------------------------------------


def test_run_1_links_the_good_ids_and_returns_the_rest(
    service_engine,
    user_id,
    i3302,  # noqa: F811
    ch6,  # noqa: F811
    task_id,
    ids,
):
    # MySQL, forms, a declined topic, MySQL again - the walk-through of 2026-10-05.
    declined = add_topic(service_engine, user_id, i3302["course_id"], "Web security", "declined")
    mysql, forms = ids[NAMES["mysql"]], ids[NAMES["forms"]]

    reply = submit(service_engine, user_id, task_id, mysql, forms, declined, mysql)

    assert (reply["status"], reply["submissions_left"]) == ("open", 2)
    assert (reply["accepted"], reply["skipped"], reply["missing"]) == ([mysql, forms], [], [])
    assert [(e["item_ordinal"], e["code"], e["field"]) for e in reply["item_errors"]] == [
        (3, "unknown_topic", "id"),
        (4, "duplicate_topic", "id"),
    ]
    assert chapter_edges(service_engine, ch6) == sorted([mysql, forms])
    with service_engine.connect() as conn:
        edge = conn.execute(select(coverage).where(coverage.c.topic_id == mysql)).all()
    linked = [e for e in edge if e.material_id == ch6][0]
    assert (linked.tier, linked.confirmed_at, linked.assessment_id) == ("inferred", None, None)


def test_a_resend_skips_what_is_linked_and_closes(service_engine, user_id, ch6, task_id, ids):  # noqa: F811
    mysql, forms = ids[NAMES["mysql"]], ids[NAMES["forms"]]
    submit(service_engine, user_id, task_id, mysql, forms, "not-a-topic")

    reply = submit(service_engine, user_id, task_id, mysql, forms)

    assert (reply["status"], reply["accepted"], reply["skipped"]) == ("closed", [], [mysql, forms])
    assert task_row(service_engine, task_id).closed_at is not None


def test_run_2_on_a_new_task_adds_no_duplicate(service_engine, user_id, ch6, task_id, ids):  # noqa: F811
    mysql, forms, files = (ids[NAMES[k]] for k in ("mysql", "forms", "files"))
    submit(service_engine, user_id, task_id, mysql, forms)
    second = start(service_engine, user_id, ch6).payload["task_id"]

    reply = submit(service_engine, user_id, second, mysql, forms, files)

    assert (reply["accepted"], reply["skipped"]) == ([files], [mysql, forms])
    assert chapter_edges(service_engine, ch6) == sorted([mysql, forms, files])


def test_an_empty_answer_closes_with_no_edge(service_engine, user_id, ch6, task_id):  # noqa: F811
    reply = submit(service_engine, user_id, task_id)  # C3: the chapter teaches none of them

    assert (reply["status"], reply["accepted"], reply["item_errors"]) == ("closed", [], [])
    assert chapter_edges(service_engine, ch6) == []


def test_an_active_topic_of_another_course_is_unknown(service_engine, user_id, ch6, task_id):  # noqa: F811
    other = add_course(service_engine, user_id, "I3301", "Databases", SEM)
    elsewhere = add_topic(service_engine, user_id, other["course_id"], "SQL joins", "active")

    reply = submit(service_engine, user_id, task_id, elsewhere)

    assert [e["code"] for e in reply["item_errors"]] == ["unknown_topic"]
    assert chapter_edges(service_engine, ch6) == []


def test_an_exam_edge_does_not_stand_in_for_the_chapter_edge(
    service_engine,
    user_id,
    ch6,  # noqa: F811
    task_id,
    ids,
):
    # MySQL already has its edge to the Final; the pair (MySQL, Chapter 6) is still missing.
    mysql = ids[NAMES["mysql"]]
    with service_engine.connect() as conn:
        exam_edges = conn.execute(
            select(func.count())
            .select_from(coverage)
            .where(coverage.c.topic_id == mysql, coverage.c.assessment_id.is_not(None))
        ).scalar_one()
    assert exam_edges == 1

    reply = submit(service_engine, user_id, task_id, mysql)

    assert reply["accepted"] == [mysql] and chapter_edges(service_engine, ch6) == [mysql]


@pytest.mark.parametrize(
    "payload",
    [{"topics": "MySQL"}, {}, [], {"topics": [], "extra": 1}],
)
def test_a_wrong_envelope_names_no_entry(service_engine, user_id, task_id, payload):
    reply = submit_generation(service_engine, user_id, task_id, payload)

    assert reply["status"] == "open"
    assert [e["item_ordinal"] for e in reply["item_errors"]] == [None]


@pytest.mark.parametrize(
    ("bad", "field"),
    [({}, "id"), ({"id": 7}, "id"), ({"id": "x", "name": "MySQL"}, "name"), ("MySQL", "item")],
)
def test_a_malformed_entry_is_an_item_error(service_engine, user_id, task_id, bad, field):
    reply = submit_generation(service_engine, user_id, task_id, {"topics": [bad]})

    assert [(e["item_ordinal"], e["field"]) for e in reply["item_errors"]] == [(1, field)]


def test_the_third_failure_closes_the_task_as_partial(service_engine, user_id, ch6, task_id, ids):  # noqa: F811
    mysql = ids[NAMES["mysql"]]
    submit(service_engine, user_id, task_id, "bad")
    submit(service_engine, user_id, task_id, mysql, "bad")

    reply = submit(service_engine, user_id, task_id, "bad")

    assert (reply["status"], reply["submissions_left"]) == ("partial", 0)
    assert chapter_edges(service_engine, ch6) == [mysql]  # what was valid stays linked


# --- the tools ------------------------------------------------------------------------------


def test_the_tools_tag_a_chapter(call, service_engine, ch6, ids):  # noqa: F811
    started = call("study_start_generation", {"kind": "tagging", "material_id": ch6})
    assert not started.is_error, started.content[0].text
    task = json.loads(started.content[0].text)["task_id"]

    done = call(
        "study_submit_generation",
        {"task_id": task, "payload": {"topics": [{"id": ids[NAMES["forms"]]}]}},
    )

    assert not done.is_error, done.content[0].text
    assert json.loads(done.content[0].text)["status"] == "closed"
    assert chapter_edges(service_engine, ch6) == [ids[NAMES["forms"]]]


def test_each_submit_logs_one_json_line(service_engine, user_id, task_id, ids, caplog):
    with caplog.at_level("INFO", logger="studysystem.services.generation"):
        submit(service_engine, user_id, task_id, ids[NAMES["mysql"]], "bad")

    line = json.loads(caplog.records[-1].getMessage())
    assert (line["event"], line["task_id"], line["status"]) == (
        "submit_generation",
        task_id,
        "open",
    )
    assert (line["accepted"], line["rejected"], line["skipped"]) == (1, 1, 0)
