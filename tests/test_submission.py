"""The generation contract, inbound (D-11, D-16, D-50 - D-55): I3302 paper 1's nine questions come
back from the host, the valid ones land as `practice_item` rows in one write unit, the rest come
back as item errors - and no resubmit, malformed or not, ever writes a question twice."""

import copy
import json

import pytest
from sqlalchemy import func, insert, select

from studysystem.db.tables import (
    evidence,
    generation_task,
    past_exam,
    practice_item,
    replacement_queue,
    submission,
    topic,
)
from studysystem.errors import StudyError
from studysystem.services.courses import add_course
from studysystem.services.generation import start_generation, submit_generation
from studysystem.services.ids import new_id, now
from studysystem.services.past_exams import add_past_exam
from studysystem.services.units import write_unit
from tests._papers import pdf_pages, write

SEM = "Semester 1 2026-2027"
REGEX = "Regular expressions"
MYSQL = "PHP + MySQL (prepared statements)"
FORMS = "PHP forms"
SESSIONS = "PHP sessions & associative arrays"
MARKS = [None, None, None, 5, 9, 7, 7, 6, 7]  # as printed: Problem I has only its total (D-48)


def item(position, marks, name, question=None, answer_key=None):
    return {
        "position": position,
        "question": question or f"Question at position {position}",
        "answer_key": answer_key,
        "marks": marks,
        "topic": {"proposed_name": name},
    }


def paper1_items() -> list[dict]:
    """Cowork's nine items, in the shape it sent: Problem I's three regex questions, then
    Problem II's Q1-Q6."""
    names = [REGEX] * 3 + [MYSQL, MYSQL, MYSQL, FORMS, SESSIONS, SESSIONS]
    return [item(p, m, n) for p, (m, n) in enumerate(zip(MARKS, names, strict=True), start=1)]


def payload(items) -> dict:
    return {"items": items}


def broken(items, at, **changes) -> list[dict]:
    """A copy of `items` with the item at position `at` changed; a value of `...` drops the
    key."""
    items = copy.deepcopy(items)
    target = items[at - 1]
    for key, value in changes.items():
        if value is ...:
            del target[key]
        else:
            target[key] = value
    return items


@pytest.fixture
def web(service_engine, user_id):
    return add_course(service_engine, user_id, "I3302", "Server-Side Web Development", SEM)


@pytest.fixture
def paper1(service_engine, user_id, web, tmp_path):
    path = write(tmp_path / "in", "I3302_First.pdf", pdf_pages(["Problem I", "Problem II"]))
    return add_past_exam(
        service_engine, user_id, "I3302", "Final exam", "first", "2020-02-17", path, "Dr. Hamze"
    )["past_exam_id"]


@pytest.fixture
def task_id(service_engine, user_id, paper1):
    return start_generation(service_engine, user_id, "transcription", paper1).payload["task_id"]


def submit(engine, user_id, task_id, body) -> dict:
    return submit_generation(engine, user_id, task_id, body)


def refused(engine, user_id, task_id, body) -> StudyError:
    with pytest.raises(StudyError) as excinfo:
        submit_generation(engine, user_id, task_id, body)
    return excinfo.value


def rows(engine, table, *where):
    with engine.connect() as conn:
        return [r._mapping for r in conn.execute(select(table).where(*where))]


def count(engine, table) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def the_task(engine, task_id):
    (row,) = rows(engine, generation_task, generation_task.c.id == task_id)
    return row


def link(engine, paper_id):
    (row,) = rows(engine, past_exam, past_exam.c.id == paper_id)
    return row["transcript_task_id"]


def items_by_position(engine):
    return sorted(rows(engine, practice_item), key=lambda r: r["position"])


def add_topic(engine, user_id, course_id, name, status="active") -> str:
    tid = new_id()
    with write_unit(engine) as conn:
        conn.execute(
            insert(topic).values(
                id=tid,
                user_id=user_id,
                course_id=course_id,
                name=name,
                status=status,
                proposed_by="profile",
                status_changed_at=now(),
                created_at=now(),
            )
        )
    return tid


# --- paper 1, first try (D-50) ----------------------------------------------------


def test_paper_1_lands_as_nine_past_exam_items(service_engine, user_id, paper1, task_id):
    submit(service_engine, user_id, task_id, payload(paper1_items()))

    got = items_by_position(service_engine)
    assert [r["position"] for r in got] == list(range(1, 10))
    assert [r["item_ordinal"] for r in got] == list(range(1, 10))  # item_ordinal = position
    assert [r["marks"] for r in got] == MARKS
    for r in got:
        assert (r["user_id"], r["task_id"], r["past_exam_id"]) == (user_id, task_id, paper1)
        assert (r["origin"], r["source_marker"], r["state"]) == (
            "past-exam",
            "from-material",
            "live",  # past-exam items skip the acceptance gate (D-22)
        )
        assert (r["answer_key"], r["answer_provenance"]) == (None, "none")
        assert r["novelty_score"] is None and r["nearest_item_id"] is None


def test_items_proposing_one_name_share_one_topic(service_engine, user_id, web, task_id):
    submit(service_engine, user_id, task_id, payload(paper1_items()))

    topics = {r["id"]: r for r in rows(service_engine, topic)}
    assert sorted(t["name"] for t in topics.values()) == sorted([REGEX, MYSQL, FORMS, SESSIONS])
    for t in topics.values():
        assert (t["status"], t["proposed_by"]) == ("proposed", "profile")  # D-51
        assert (t["user_id"], t["course_id"]) == (user_id, web["course_id"])
    got = items_by_position(service_engine)
    assert len({r["topic_id"] for r in got[:3]}) == 1  # the three regex questions
    assert topics[got[0]["topic_id"]]["name"] == REGEX


def test_a_clean_submission_closes_the_task_and_links_the_paper(
    service_engine, user_id, paper1, task_id
):
    reply = submit(service_engine, user_id, task_id, payload(paper1_items()))

    assert reply == {
        "task_id": task_id,
        "submission": 1,
        "status": "closed",
        "accepted": list(range(1, 10)),
        "skipped": [],
        "missing": [],
        "item_errors": [],
        "submissions_left": 0,
    }
    row = the_task(service_engine, task_id)
    assert row["status"] == "closed" and row["closed_at"] is not None
    assert link(service_engine, paper1) == task_id  # D-45


def test_one_submission_row_records_it(service_engine, user_id, task_id):
    body = payload(paper1_items())
    submit(service_engine, user_id, task_id, body)

    (row,) = rows(service_engine, submission)
    assert (row["user_id"], row["task_id"], row["ordinal"]) == (user_id, task_id, 1)
    assert json.loads(row["payload"]) == body
    assert json.loads(row["validation_result"]) == {"ok": True, "item_errors": [], "skipped": []}
    assert (row["accepted_count"], row["rejected_count"]) == (9, 0)


def test_each_item_is_e1_evidence(service_engine, user_id, task_id):
    submit(service_engine, user_id, task_id, payload(paper1_items()))

    got = rows(service_engine, evidence)
    item_ids = {r["id"] for r in rows(service_engine, practice_item)}
    assert {r["practice_item_id"] for r in got} == item_ids and len(got) == 9
    for r in got:
        assert (r["user_id"], r["study_tier"], r["locator"]) == (user_id, "E1", None)
        assert r["memory_item_id"] is None and r["observation_id"] is None


def test_a_printed_answer_key_is_verified_source(service_engine, user_id, task_id):
    items = broken(paper1_items(), 1, answer_key="^[a-z]+$")

    submit(service_engine, user_id, task_id, payload(items))

    first, second = items_by_position(service_engine)[:2]
    assert (first["answer_key"], first["answer_provenance"]) == ("^[a-z]+$", "verified-source")
    assert second["answer_provenance"] == "none"


def test_an_empty_answer_key_is_an_item_error(service_engine, user_id, task_id):
    """ "" is not a printed answer: the host must send null (D-57)."""
    items = broken(paper1_items(), 2, answer_key="")

    reply = submit(service_engine, user_id, task_id, payload(items))

    (err,) = reply["item_errors"]
    assert (err["item_ordinal"], err["code"], err["field"]) == (2, "minLength", "answer_key")
    assert 2 not in [r["position"] for r in rows(service_engine, practice_item)]


def test_items_without_answer_key_or_marks_keys_are_unknown(service_engine, user_id, task_id):
    """Neither key is required by the schema: left out means null, not a crash."""
    items = broken(paper1_items(), 4, answer_key=..., marks=...)

    submit(service_engine, user_id, task_id, payload(items))

    q4 = items_by_position(service_engine)[3]
    assert (q4["answer_key"], q4["answer_provenance"], q4["marks"]) == (None, "none", None)


# --- topics (D-51) ---------------------------------------------------------------


def test_a_name_matching_a_live_topic_is_reused_ignoring_case(
    service_engine, user_id, web, task_id
):
    existing = add_topic(service_engine, user_id, web["course_id"], REGEX)
    items = broken(paper1_items(), 2, topic={"proposed_name": "  regular EXPRESSIONS "})

    submit(service_engine, user_id, task_id, payload(items))

    got = items_by_position(service_engine)
    assert {r["topic_id"] for r in got[:3]} == {existing}
    assert count(service_engine, topic) == 4  # the regex topic was not proposed again


@pytest.mark.parametrize("name", ["", "   "])
def test_a_blank_topic_name_is_an_item_error(service_engine, user_id, task_id, name):
    """A name that trims to "" would create a nameless topic (D-51)."""
    items = broken(paper1_items(), 2, topic={"proposed_name": name})

    reply = submit(service_engine, user_id, task_id, payload(items))

    (err,) = reply["item_errors"]
    assert (err["item_ordinal"], err["field"]) == (2, "topic")
    assert "" not in {r["name"].strip() for r in rows(service_engine, topic)}


def test_an_existing_topic_id_is_used(service_engine, user_id, web, task_id):
    existing = add_topic(service_engine, user_id, web["course_id"], "Forms and validation")
    items = broken(paper1_items(), 7, topic={"id": existing})

    submit(service_engine, user_id, task_id, payload(items))

    assert items_by_position(service_engine)[6]["topic_id"] == existing
    assert FORMS not in {r["name"] for r in rows(service_engine, topic)}


@pytest.mark.parametrize("where", ["other course", "declined", "unknown"])
def test_a_topic_id_that_is_not_a_live_topic_of_the_course_is_rejected(
    service_engine, user_id, web, task_id, where
):
    if where == "other course":
        other = add_course(service_engine, user_id, "I3301", "Software Engineering", SEM)
        tid = add_topic(service_engine, user_id, other["course_id"], "UML")
    elif where == "declined":
        tid = add_topic(service_engine, user_id, web["course_id"], "Old name", status="declined")
    else:
        tid = new_id()
    items = broken(paper1_items(), 4, topic={"id": tid})

    reply = submit(service_engine, user_id, task_id, payload(items))

    (err,) = reply["item_errors"]
    assert (err["item_ordinal"], err["code"], err["field"]) == (4, "unknown_topic", "topic")
    assert 4 not in [r["position"] for r in rows(service_engine, practice_item)]


def test_a_rejected_item_proposes_no_topic(service_engine, user_id, task_id):
    items = broken(paper1_items(), 7, marks=-7)  # the only PHP forms question

    submit(service_engine, user_id, task_id, payload(items))

    assert FORMS not in {r["name"] for r in rows(service_engine, topic)}


# --- partial accept and resubmits (D-11, D-52, D-53) -------------------------------


def two_bad() -> list[dict]:
    """Paper 1 with Q5's marks negative and Q8's question missing."""
    return broken(broken(paper1_items(), 5, marks=-9), 8, question=...)


def test_bad_items_come_back_and_the_good_ones_land(service_engine, user_id, paper1, task_id):
    reply = submit(service_engine, user_id, task_id, payload(two_bad()))

    assert reply["status"] == "open"
    assert reply["accepted"] == [1, 2, 3, 4, 6, 7, 9]
    assert [(e["item_ordinal"], e["code"], e["field"]) for e in reply["item_errors"]] == [
        (5, "minimum", "marks"),
        (8, "required", "question"),
    ]
    assert all(e["message"] for e in reply["item_errors"])
    assert reply["submissions_left"] == 2
    assert count(service_engine, practice_item) == 7
    assert link(service_engine, paper1) is None  # not transcribed until it closes
    (row,) = rows(service_engine, submission)
    assert (row["accepted_count"], row["rejected_count"]) == (7, 2)
    assert json.loads(row["validation_result"])["ok"] is False


def test_resending_all_nine_skips_the_accepted_and_closes(service_engine, user_id, paper1, task_id):
    submit(service_engine, user_id, task_id, payload(two_bad()))

    reply = submit(service_engine, user_id, task_id, payload(paper1_items()))

    assert (reply["submission"], reply["status"]) == (2, "closed")
    assert reply["accepted"] == [5, 8]
    assert reply["skipped"] == [1, 2, 3, 4, 6, 7, 9]
    assert reply["item_errors"] == []
    assert count(service_engine, practice_item) == 9
    assert link(service_engine, paper1) == task_id
    second = rows(service_engine, submission, submission.c.ordinal == 2)[0]
    assert json.loads(second["validation_result"])["skipped"] == [1, 2, 3, 4, 6, 7, 9]
    assert (second["accepted_count"], second["rejected_count"]) == (2, 0)


def test_resending_only_the_fixed_items_closes(service_engine, user_id, task_id):
    submit(service_engine, user_id, task_id, payload(two_bad()))
    fixed = [i for i in paper1_items() if i["position"] in (5, 8)]

    reply = submit(service_engine, user_id, task_id, payload(fixed))

    assert (reply["status"], reply["accepted"], reply["skipped"]) == ("closed", [5, 8], [])
    assert count(service_engine, practice_item) == 9


def test_a_malformed_resubmit_duplicates_nothing(service_engine, user_id, paper1, task_id):
    """The 1.10 *Done when*: a resubmit that is still broken lands what it can and writes no
    question twice."""
    submit(service_engine, user_id, task_id, payload(two_bad()))
    still_bad = broken(paper1_items(), 5, marks=-9)  # Q8 fixed, Q5 not

    reply = submit(service_engine, user_id, task_id, payload(still_bad))

    assert (reply["status"], reply["accepted"]) == ("open", [8])
    assert reply["skipped"] == [1, 2, 3, 4, 6, 7, 9]
    assert [e["item_ordinal"] for e in reply["item_errors"]] == [5]
    positions = [r["position"] for r in rows(service_engine, practice_item)]
    assert sorted(positions) == [1, 2, 3, 4, 6, 7, 8, 9]  # 8 rows, each position once
    assert count(service_engine, evidence) == 8


def test_an_accepted_item_is_never_rewritten(service_engine, user_id, task_id):
    submit(service_engine, user_id, task_id, payload(two_bad()))
    rewritten = broken(paper1_items(), 1, question="Something else entirely")

    submit(service_engine, user_id, task_id, payload(rewritten))

    assert items_by_position(service_engine)[0]["question"] == "Question at position 1"


def test_two_items_with_one_position_are_both_rejected(service_engine, user_id, task_id):
    items = broken(paper1_items(), 9, position=8)

    reply = submit(service_engine, user_id, task_id, payload(items))

    assert [(e["item_ordinal"], e["code"]) for e in reply["item_errors"]] == [
        (8, "duplicate_position"),
        (9, "duplicate_position"),
    ]
    assert sorted(r["position"] for r in rows(service_engine, practice_item)) == list(range(1, 8))
    assert reply["status"] == "open"


def test_a_gap_keeps_the_task_open(service_engine, user_id, paper1, task_id):
    """A clean submission that skipped a question does not close the task (D-53)."""
    without_3 = [i for i in paper1_items() if i["position"] != 3]

    reply = submit(service_engine, user_id, task_id, payload(without_3))

    assert (reply["status"], reply["item_errors"], reply["missing"]) == ("open", [], [3])
    assert link(service_engine, paper1) is None

    reply = submit(service_engine, user_id, task_id, payload([paper1_items()[2]]))

    assert (reply["status"], reply["accepted"], reply["missing"]) == ("closed", [3], [])


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"items": []},
        {"items": "all nine"},
        {"items": [item(1, None, REGEX)], "notes": "extra"},
        [item(1, None, REGEX)],
    ],
    ids=["no-items", "empty", "not-a-list", "extra-key", "bare-list"],
)
def test_a_broken_envelope_is_one_error_and_uses_a_submission(
    service_engine, user_id, task_id, body
):
    reply = submit(service_engine, user_id, task_id, body)

    (err,) = reply["item_errors"]
    assert err["item_ordinal"] is None and err["code"] and err["message"]
    assert (reply["status"], reply["accepted"], reply["submissions_left"]) == ("open", [], 2)
    assert count(service_engine, practice_item) == 0
    (row,) = rows(service_engine, submission)
    assert json.loads(row["payload"]) == body  # stored as received - the eval dataset (D-12)
    assert (row["accepted_count"], row["rejected_count"]) == (0, 0)


# --- the three-submission cap (D-16, D-53) -------------------------------------------


def test_the_third_failed_submission_closes_the_task_partial(
    service_engine, user_id, paper1, task_id
):
    replies = [submit(service_engine, user_id, task_id, payload(two_bad())) for _ in range(3)]

    reply = replies[-1]
    assert (reply["submission"], reply["status"], reply["submissions_left"]) == (3, "partial", 0)
    row = the_task(service_engine, task_id)
    assert row["status"] == "partial" and row["closed_at"] is not None
    assert link(service_engine, paper1) == task_id  # it closed with items
    assert count(service_engine, practice_item) == 7
    assert count(service_engine, replacement_queue) == 0  # nothing to regenerate (D-53)


def test_three_submissions_with_nothing_accepted_leave_the_paper_open_to_a_new_task(
    service_engine, user_id, paper1, task_id
):
    for _ in range(3):
        submit(service_engine, user_id, task_id, {"items": []})

    assert the_task(service_engine, task_id)["status"] == "partial"
    assert link(service_engine, paper1) is None
    fresh = start_generation(service_engine, user_id, "transcription", paper1).payload
    assert fresh["task_id"] != task_id


@pytest.mark.parametrize("ending", ["closed", "partial"])
def test_a_finished_task_refuses_a_submit_and_writes_nothing(
    service_engine, user_id, task_id, ending
):
    if ending == "closed":
        submit(service_engine, user_id, task_id, payload(paper1_items()))
    else:
        for _ in range(3):
            submit(service_engine, user_id, task_id, payload(two_bad()))
    before = (count(service_engine, submission), count(service_engine, practice_item))

    err = refused(service_engine, user_id, task_id, payload(paper1_items()))

    assert (err.code, err.field_errors[0]["field"]) == ("task_closed", "task_id")
    assert (count(service_engine, submission), count(service_engine, practice_item)) == before


# --- refusals --------------------------------------------------------------------


def test_an_unknown_task_is_not_found(service_engine, user_id, task_id):
    err = refused(service_engine, user_id, new_id(), payload(paper1_items()))

    assert (err.code, err.field_errors[0]["field"]) == ("not_found", "task_id")
    assert count(service_engine, submission) == 0


def test_another_students_task_is_not_found(service_engine, user_id, other_user_id, task_id):
    err = refused(service_engine, other_user_id, task_id, payload(paper1_items()))

    assert err.code == "not_found"
    assert count(service_engine, submission) == 0


# --- bytes (D-55) ----------------------------------------------------------------


def size(document) -> int:
    return len(json.dumps(document).encode("utf-8"))


def test_the_byte_counters_are_running_totals(service_engine, user_id, task_id):
    sent_at_start = the_task(service_engine, task_id)["bytes_sent"]
    first_body, second_body = payload(two_bad()), payload(paper1_items())

    first = submit(service_engine, user_id, task_id, first_body)
    second = submit(service_engine, user_id, task_id, second_body)

    subs = sorted(rows(service_engine, submission), key=lambda r: r["ordinal"])
    assert [s["bytes_returned"] for s in subs] == [size(first_body), size(second_body)]
    assert [s["bytes_sent"] for s in subs] == [size(first), size(second)]
    row = the_task(service_engine, task_id)
    assert row["bytes_returned"] == size(first_body) + size(second_body)
    assert row["bytes_sent"] == sent_at_start + size(first) + size(second)


# --- the log line ------------------------------------------------------------------


def test_each_submit_logs_one_json_line(service_engine, user_id, task_id, caplog):
    caplog.set_level("INFO", logger="studysystem")

    reply = submit(service_engine, user_id, task_id, payload(two_bad()))

    (line,) = [json.loads(r.getMessage()) for r in caplog.records]
    assert line == {
        "event": "submit_generation",
        "task_id": task_id,
        "submission": 1,
        "status": "open",
        "accepted": 7,
        "rejected": 2,
        "skipped": 0,
        "bytes": size(payload(two_bad())),
    }
    assert reply["submission"] == 1


# --- the tool ----------------------------------------------------------------------


def test_the_tool_takes_a_submission(call, service_engine, task_id):
    result = call(
        "study_submit_generation", {"task_id": task_id, "payload": payload(paper1_items())}
    )

    assert not result.is_error, result.content[0].text
    assert json.loads(result.content[0].text)["status"] == "closed"
    assert count(service_engine, practice_item) == 9


def test_the_tool_returns_a_refusal_as_an_error_result(call, task_id):
    result = call(
        "study_submit_generation", {"task_id": new_id(), "payload": payload(paper1_items())}
    )

    assert result.is_error
    assert json.loads(result.content[0].text)["code"] == "not_found"
