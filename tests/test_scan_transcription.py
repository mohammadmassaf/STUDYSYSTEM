"""A scanned paper's transcription (2.6, D-210): the pictures go out, each item names its page, the
reply reads the paper back, and the paper counts only after the student confirms it. I3301's
2022-04-13 Final is the example: three camera pages, no text layer."""

import pytest
from sqlalchemy import select

from studysystem.db.tables import past_exam, practice_item
from studysystem.errors import StudyError
from studysystem.services.courses import add_course
from studysystem.services.generation import (
    confirm_transcription,
    start_generation,
    submit_generation,
)
from studysystem.services.past_exams import add_past_exam
from studysystem.services.profiles import paper_set
from tests._papers import pdf_pages, write

SEM = "Semester 1 2026-2027"
DATE = "2022-04-13"
HEADER = {"date": DATE, "course": "I3301", "instructor": None}


@pytest.fixture
def course(service_engine, user_id):
    return add_course(service_engine, user_id, "I3301", "Computer Architecture", SEM)


def register(engine, user_id, tmp_path, name, pages, pictured=(), date=DATE):
    path = write(tmp_path / name, f"{name}.pdf", pdf_pages(pages, pictured=pictured))
    return add_past_exam(engine, user_id, "I3301", "Final exam", "first", date, path)[
        "past_exam_id"
    ]


@pytest.fixture
def scan(service_engine, user_id, course, tmp_path):
    """Three pages, no text, a picture on each - the shape of a camera scan."""
    return register(service_engine, user_id, tmp_path, "scan", [None] * 3, pictured={1, 2, 3})


@pytest.fixture
def task_id(service_engine, user_id, scan):
    return start_generation(service_engine, user_id, "transcription", scan).payload["task_id"]


def item(position, page, **changes):
    body = {
        "position": position,
        "page": page,
        "question": f"Question {position}",
        "answer_key": None,
        "marks": 5,
        "topic": {"proposed_name": "Pipelining"},
    }
    return {**body, **changes}


def good_items():
    return [item(1, 1), item(2, 2), item(3, 3)]


def submit(engine, user_id, task_id, items=None, header=HEADER):
    body = {"items": good_items() if items is None else items}
    if header is not None:
        body["header"] = header
    return submit_generation(engine, user_id, task_id, body)


def the_paper(engine, paper_id):
    with engine.connect() as conn:
        return conn.execute(select(past_exam).where(past_exam.c.id == paper_id)).one()


def in_derive_set(engine, user_id, paper_id):
    with engine.connect() as conn:
        slot_id = the_paper(engine, paper_id).slot_id
        return paper_id in [pid for pid, _ in paper_set(conn, user_id, slot_id)]


def refused(call, *args) -> StudyError:
    with pytest.raises(StudyError) as excinfo:
        call(*args)
    return excinfo.value


# --- outbound: the pictures go out ------------------------------------------------


def test_a_scan_starts_with_a_picture_on_every_page(service_engine, user_id, scan):
    task = start_generation(service_engine, user_id, "transcription", scan)

    assert [p["image"] for p in task.payload["content"]] == [1, 2, 3]
    assert len(task.pictures) == 3


# --- inbound: every item names its page --------------------------------------------


def test_a_scan_item_with_no_page_is_an_item_error(service_engine, user_id, task_id):
    items = good_items()
    del items[1]["page"]

    reply = submit(service_engine, user_id, task_id, items)

    (err,) = reply["item_errors"]
    assert (err["item_ordinal"], err["field"]) == (2, "page")
    assert reply["accepted"] == [1, 3]


def test_a_page_past_the_files_last_is_an_item_error(service_engine, user_id, task_id):
    reply = submit(service_engine, user_id, task_id, [item(1, 1), item(2, 2), item(3, 4)])

    (err,) = reply["item_errors"]
    assert (err["item_ordinal"], err["field"]) == (3, "page")


def test_the_item_page_is_stored(service_engine, user_id, task_id):
    submit(service_engine, user_id, task_id)

    with service_engine.connect() as conn:
        rows = conn.execute(select(practice_item).order_by(practice_item.c.position)).all()
    assert [r.page for r in rows] == [1, 2, 3]


# --- the flag, not a refusal --------------------------------------------------------


def test_a_page_with_no_item_is_flagged_and_the_task_still_closes(service_engine, user_id, task_id):
    reply = submit(service_engine, user_id, task_id, [item(1, 1), item(2, 3)])

    assert reply["pages_empty"] == [2]
    assert reply["status"] == "closed"


def test_pages_all_covered_flag_nothing(service_engine, user_id, task_id):
    assert submit(service_engine, user_id, task_id)["pages_empty"] == []


# --- closed is not confirmed --------------------------------------------------------


def test_a_closed_scan_is_linked_but_not_confirmed(service_engine, user_id, scan, task_id):
    submit(service_engine, user_id, task_id)

    paper = the_paper(service_engine, scan)
    assert paper.transcript_task_id == task_id
    assert paper.transcript_confirmed_at is None


def test_an_unconfirmed_scan_is_not_in_the_derive_set(service_engine, user_id, scan, task_id):
    submit(service_engine, user_id, task_id)

    assert not in_derive_set(service_engine, user_id, scan)


def test_confirming_stamps_it_and_derive_then_counts_it(service_engine, user_id, scan, task_id):
    submit(service_engine, user_id, task_id)

    confirm_transcription(service_engine, user_id, scan)

    assert the_paper(service_engine, scan).transcript_confirmed_at is not None
    assert in_derive_set(service_engine, user_id, scan)


def test_confirming_twice_is_refused(service_engine, user_id, scan, task_id):
    submit(service_engine, user_id, task_id)
    confirm_transcription(service_engine, user_id, scan)

    err = refused(confirm_transcription, service_engine, user_id, scan)

    assert err.code == "already_confirmed"


def test_a_paper_never_transcribed_cannot_be_confirmed(service_engine, user_id, scan):
    err = refused(confirm_transcription, service_engine, user_id, scan)

    assert err.code == "not_transcribed"


# --- a typed paper is unchanged -----------------------------------------------------


def test_a_text_layer_paper_is_confirmed_at_submit(service_engine, user_id, course, tmp_path):
    typed = register(service_engine, user_id, tmp_path, "typed", ["Q1", "Q2"], date="2023-03-16")
    typed_task = start_generation(service_engine, user_id, "transcription", typed).payload[
        "task_id"
    ]
    body = {"items": [item(1, None), item(2, None)]}
    for entry in body["items"]:
        del entry["page"]

    submit_generation(service_engine, user_id, typed_task, body)

    assert the_paper(service_engine, typed).transcript_confirmed_at is not None
    assert in_derive_set(service_engine, user_id, typed)


# --- the read-back ------------------------------------------------------------------


def test_the_reply_reads_each_question_back_beside_its_page(service_engine, user_id, task_id):
    reply = submit(service_engine, user_id, task_id)

    assert reply["read_back"]["items"] == [
        {"position": 1, "page": 1, "question": "Question 1"},
        {"position": 2, "page": 2, "question": "Question 2"},
        {"position": 3, "page": 3, "question": "Question 3"},
    ]


def test_the_header_is_shown_beside_the_registered_row_and_stored_nowhere(
    service_engine, user_id, scan, task_id
):
    misread = {"date": "2022-04-18", "course": "I3301", "instructor": "Dr. X"}

    reply = submit(service_engine, user_id, task_id, header=misread)

    assert reply["read_back"]["header"] == {
        "read": misread,
        "registered": {"date": DATE, "course": "I3301", "instructor": None},
    }
    assert the_paper(service_engine, scan).session_date == DATE  # D-43: the row is not rewritten


def test_a_scan_with_no_header_is_kept_and_reads_back_null(service_engine, user_id, task_id):
    reply = submit(service_engine, user_id, task_id, header=None)

    assert reply["status"] == "closed"
    assert reply["read_back"]["header"]["read"] is None


def test_a_header_of_the_wrong_shape_is_an_envelope_error(service_engine, user_id, task_id):
    reply = submit(service_engine, user_id, task_id, header={"date": 20220413})

    (err,) = reply["item_errors"]
    assert err["item_ordinal"] is None
    assert reply["accepted"] == []


def test_a_typed_papers_page_is_dropped_never_kept(service_engine, user_id, course, tmp_path):
    typed = register(service_engine, user_id, tmp_path, "typed", ["Q1"], date="2023-03-16")
    typed_task = start_generation(service_engine, user_id, "transcription", typed).payload[
        "task_id"
    ]

    reply = submit_generation(service_engine, user_id, typed_task, {"items": [item(1, 7)]})

    with service_engine.connect() as conn:
        (page,) = conn.execute(select(practice_item.c.page)).scalars().all()
    assert page is None
    assert "read_back" not in reply  # a typed paper's reply is as it was


def test_an_unconfirmed_scan_restarted_says_it_waits_for_his_confirm(
    service_engine, user_id, scan, task_id
):
    submit(service_engine, user_id, task_id)

    err = refused(start_generation, service_engine, user_id, "transcription", scan)

    assert err.code == "already_transcribed"
    assert "study_confirm_transcription" in err.fix
