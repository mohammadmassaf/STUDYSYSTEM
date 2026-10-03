"""The note task, inbound (1.14; D-11, D-52, D-70, D-73): Chapter 6's note comes back as one
`note` row. The envelope is checked once, then each entry alone - schema, section 1 only, the
first copy of a section kept, a section already on the task skipped - and a task gets three
tries. Never a topic, never an edge."""

import json

import pytest
from sqlalchemy import func, select

from studysystem.db.tables import coverage, generation_task, note, submission, topic
from studysystem.errors import StudyError
from studysystem.services.generation import submit_generation
from tests.test_note_generation import ch6, i3302, start, web  # noqa: F401 - fixtures

BODY = "# PHP & MySQL\n\nmysqli_connect() opens the connection ..."


@pytest.fixture
def task_id(service_engine, user_id, ch6):  # noqa: F811
    return start(service_engine, user_id, ch6).payload["task_id"]


def submit(engine, user_id, task_id, payload):
    return submit_generation(engine, user_id, task_id, payload)


def entry(section=1, body=BODY):
    return {"section": section, "body": body}


def note_rows(engine):
    with engine.connect() as conn:
        return conn.execute(select(note).order_by(note.c.id)).all()


def task_row(engine, task_id):
    with engine.connect() as conn:
        return conn.execute(select(generation_task).where(generation_task.c.id == task_id)).one()


def count(engine, table) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def submissions(engine, task_id):
    """This task's submissions - the fixture's three transcriptions have their own."""
    with engine.connect() as conn:
        return conn.execute(
            select(submission).where(submission.c.task_id == task_id).order_by(submission.c.ordinal)
        ).all()


# --- the happy path -----------------------------------------------------------------


def test_chapter_6_s_note_lands_and_closes_the_task(
    service_engine,
    user_id,
    i3302,  # noqa: F811
    ch6,  # noqa: F811
    task_id,
):
    reply = submit(service_engine, user_id, task_id, {"notes": [entry()]})

    # no vault on a test machine (conftest): the note saves, its export says why it did not
    (export,) = reply.pop("exports")
    assert (export["section"], export["exported_path"]) == (1, None)
    assert "STUDYSYSTEM_VAULT_DIR" in export["fix"]
    assert reply == {
        "task_id": task_id,
        "submission": 1,
        "status": "closed",
        "accepted": [1],
        "skipped": [],
        "missing": [],
        "item_errors": [],
        "submissions_left": 0,
    }
    (row,) = note_rows(service_engine)
    assert (row.material_id, row.task_id, row.section_ordinal) == (ch6, task_id, 1)
    assert row.exam_profile_id == i3302["profile_id"]  # the task's profile, v1 (D-71)
    assert row.body == BODY
    assert row.exported_path is None  # the export fills it, after commit (D-72)
    task = task_row(service_engine, task_id)
    assert task.status == "closed" and task.closed_at is not None


def test_the_submission_is_recorded_with_its_counts(service_engine, user_id, task_id):
    payload = {"notes": [entry()]}
    submit(service_engine, user_id, task_id, payload)

    (sub,) = submissions(service_engine, task_id)
    assert (sub.ordinal, sub.accepted_count, sub.rejected_count) == (1, 1, 0)
    assert json.loads(sub.payload) == payload
    assert json.loads(sub.validation_result) == {"ok": True, "item_errors": [], "skipped": []}
    assert task_row(service_engine, task_id).bytes_returned == sub.bytes_returned > 0


def test_a_note_writes_no_topic_and_no_edge(service_engine, user_id, task_id):
    """D-70: a note links to its material only."""
    topics_before = count(service_engine, topic)
    edges_before = count(service_engine, coverage)

    submit(service_engine, user_id, task_id, {"notes": [entry()]})

    assert count(service_engine, topic) == topics_before
    assert count(service_engine, coverage) == edges_before


# --- entries that fail alone ---------------------------------------------------------


def test_a_blank_body_is_an_item_error_and_the_task_stays_open(service_engine, user_id, task_id):
    reply = submit(service_engine, user_id, task_id, {"notes": [entry(body="   ")]})

    assert (reply["status"], reply["missing"], reply["submissions_left"]) == ("open", [1], 2)
    (err,) = reply["item_errors"]
    assert (err["item_ordinal"], err["field"]) == (1, "body")
    assert note_rows(service_engine) == []


def test_section_2_is_an_item_error_and_section_1_still_lands(service_engine, user_id, task_id):
    reply = submit(service_engine, user_id, task_id, {"notes": [entry(), entry(section=2)]})

    assert reply["accepted"] == [1]
    (err,) = reply["item_errors"]
    assert (err["item_ordinal"], err["code"], err["field"]) == (2, "unknown_section", "section")
    assert reply["status"] == "open"  # an error keeps it open, even with nothing missing
    assert len(note_rows(service_engine)) == 1


def test_section_1_twice_keeps_the_first_copy(service_engine, user_id, task_id):
    reply = submit(
        service_engine, user_id, task_id, {"notes": [entry(body="first"), entry(body="second")]}
    )

    assert reply["accepted"] == [1]
    (err,) = reply["item_errors"]
    assert (err["item_ordinal"], err["code"]) == (2, "duplicate_section")
    (row,) = note_rows(service_engine)
    assert row.body == "first"


@pytest.mark.parametrize(
    ("bad", "field"),
    [
        ({"section": 0, "body": BODY}, "section"),
        ({"section": "1", "body": BODY}, "section"),
        ({"body": BODY}, "section"),
        ({"section": 1, "body": BODY, "topic": "MySQL"}, "topic"),  # D-70: no topic field
    ],
)
def test_a_malformed_entry_is_an_item_error(service_engine, user_id, task_id, bad, field):
    reply = submit(service_engine, user_id, task_id, {"notes": [bad]})

    assert [e["field"] for e in reply["item_errors"]] == [field]
    assert all(e["item_ordinal"] == 1 for e in reply["item_errors"])
    assert note_rows(service_engine) == []


@pytest.mark.parametrize(
    "payload",
    [
        {"items": [{"position": 1}]},  # a transcription's shape
        {"notes": []},
        {"notes": "the whole note"},
        "# PHP & MySQL",
    ],
)
def test_a_wrong_envelope_names_no_entry(service_engine, user_id, task_id, payload):
    reply = submit(service_engine, user_id, task_id, payload)

    (err,) = reply["item_errors"]
    assert err["item_ordinal"] is None
    assert (reply["status"], reply["missing"]) == ("open", [1])
    (sub,) = submissions(service_engine, task_id)
    assert sub.rejected_count == 0  # D-52


# --- retries --------------------------------------------------------------------------


def test_a_resent_section_1_is_skipped_and_closes_the_task(service_engine, user_id, task_id):
    """Submission 1 lands section 1 but errs on section 2; the host resends everything."""
    submit(service_engine, user_id, task_id, {"notes": [entry(body="kept"), entry(section=2)]})

    reply = submit(service_engine, user_id, task_id, {"notes": [entry(body="resent")]})

    assert (reply["submission"], reply["status"]) == (2, "closed")
    assert (reply["accepted"], reply["skipped"], reply["missing"]) == ([], [1], [])
    (row,) = note_rows(service_engine)
    assert row.body == "kept"  # skipped, never rewritten


def test_the_third_failure_closes_the_task_as_partial(service_engine, user_id, task_id):
    for expected in ("open", "open", "partial"):
        reply = submit(service_engine, user_id, task_id, {"notes": [entry(body=" ")]})
        assert reply["status"] == expected

    task = task_row(service_engine, task_id)
    assert task.status == "partial" and task.closed_at is not None
    assert reply["submissions_left"] == 0
    assert len(submissions(service_engine, task_id)) == 3


def test_a_closed_task_takes_no_more_submissions(service_engine, user_id, task_id):
    submit(service_engine, user_id, task_id, {"notes": [entry()]})

    with pytest.raises(StudyError) as excinfo:
        submit(service_engine, user_id, task_id, {"notes": [entry()]})
    assert excinfo.value.code == "task_closed"
    assert len(note_rows(service_engine)) == 1


def test_a_partial_task_lets_the_material_start_again(
    service_engine,
    user_id,
    ch6,  # noqa: F811
    task_id,
):
    for _ in range(3):
        submit(service_engine, user_id, task_id, {"notes": [entry(body=" ")]})

    again = start(service_engine, user_id, ch6).payload["task_id"]
    reply = submit(service_engine, user_id, again, {"notes": [entry()]})

    assert again != task_id
    assert reply["status"] == "closed"
    assert len(note_rows(service_engine)) == 1


# --- the tool --------------------------------------------------------------------------


def test_submit_through_the_tool(call, service_engine, task_id):
    result = call("study_submit_generation", {"task_id": task_id, "payload": {"notes": [entry()]}})

    assert not result.is_error, result.content[0].text
    assert json.loads(result.content[0].text)["status"] == "closed"
