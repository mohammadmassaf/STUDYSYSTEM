"""The generation contract, outbound (D-11, D-18, D-45 - D-49): I3302's first-session paper leaves
the server as its pages' text inside a task - plus a picture of each page with a figure on it - one
`generation_task` row records it, and every refusal writes nothing."""

import base64
import json

import pytest
from sqlalchemy import func, insert, select, update

from studysystem.db.tables import generation_task, past_exam, topic
from studysystem.errors import StudyError
from studysystem.services.courses import add_course
from studysystem.services.generation import TRANSCRIPTION_SCHEMA, measure, start_generation
from studysystem.services.ids import new_id, now
from studysystem.services.past_exams import add_past_exam
from studysystem.services.units import write_unit
from tests._papers import pdf_bytes, pdf_pages, write

SEM = "Semester 1 2026-2027"
PAGES = ["Problem I (9 pts) regular expressions", "Problem II (6 pts) sessions", "Problem III"]


@pytest.fixture
def web(service_engine, user_id):
    return add_course(service_engine, user_id, "I3302", "Server-Side Web Development", SEM)


def register(engine, owner, folder, name, data, date="2020-02-17"):
    """One paper under `owner`'s I3302 Final exam; returns its past_exam_id."""
    path = write(folder, name, data)
    return add_past_exam(
        engine, owner, "I3302", "Final exam", "first", date, path, "Dr. Mohamad Hamze"
    )["past_exam_id"]


@pytest.fixture
def paper1(service_engine, user_id, web, tmp_path):
    """Paper 1: three pages with a text layer."""
    return register(service_engine, user_id, tmp_path / "in", "I3302_First.pdf", pdf_pages(PAGES))


@pytest.fixture
def figured(service_engine, user_id, web, tmp_path):
    """A paper whose page 2 carries a figure - an image on a typed page, like paper 1's ER
    diagram."""
    data = pdf_pages(PAGES, pictured={2})
    return register(service_engine, user_id, tmp_path / "in", "figured.pdf", data, "2020-09-14")


def start_task(engine, user_id, paper_id, kind="transcription"):
    return start_generation(engine, user_id, kind, paper_id)


def start(engine, user_id, paper_id, kind="transcription"):
    """The payload alone - the JSON the host reads."""
    return start_task(engine, user_id, paper_id, kind).payload


def refused(engine, user_id, kind, paper_id) -> StudyError:
    with pytest.raises(StudyError) as excinfo:
        start_generation(engine, user_id, kind, paper_id)
    return excinfo.value


def tasks(engine):
    with engine.connect() as conn:
        return [r._mapping for r in conn.execute(select(generation_task))]


def task_count(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(generation_task)).scalar_one()


# --- the task -----------------------------------------------------------------


def test_paper_1_leaves_as_its_pages_text(service_engine, user_id, paper1):
    payload = start(service_engine, user_id, paper1)

    assert set(payload) == {"task_id", "content", "schema", "rules"}
    assert [p["page"] for p in payload["content"]] == [1, 2, 3]
    assert [p["text"] for p in payload["content"]] == PAGES
    assert [p["image"] for p in payload["content"]] == [None, None, None]  # no figures
    assert payload["schema"] == TRANSCRIPTION_SCHEMA
    assert payload["rules"]["topics"] == []  # I3302 has no topics yet
    assert payload["rules"]["steps"]


def test_one_task_row_records_it(service_engine, user_id, paper1):
    payload = start(service_engine, user_id, paper1)

    (row,) = tasks(service_engine)
    assert row["id"] == payload["task_id"]
    assert row["user_id"] == user_id
    assert (row["kind"], row["scope_type"], row["scope_id"]) == (
        "transcription",
        "past-exam",
        paper1,
    )
    assert (row["route"], row["status"]) == ("host", "open")
    assert row["exam_profile_id"] is None and row["bytes_returned"] == 0
    assert row["closed_at"] is None and row["superseded_by"] is None


def test_bytes_sent_is_the_payload_size(service_engine, user_id, paper1):
    task = start_task(service_engine, user_id, paper1)

    (row,) = tasks(service_engine)
    assert task.pictures == []
    assert row["bytes_sent"] == measure(task.payload, []) > 0


# --- pictures (D-49) ------------------------------------------------------------


def test_a_page_with_a_figure_also_goes_out_as_a_picture(service_engine, user_id, figured):
    task = start_task(service_engine, user_id, figured)

    content = task.payload["content"]
    assert [p["image"] for p in content] == [None, 1, None]
    assert [p["text"].strip() for p in content] == PAGES  # the text still goes, every page
    (picture,) = task.pictures
    assert picture.mime_type == "image/jpeg"
    assert base64.b64decode(picture.data).startswith(b"\xff\xd8\xff")  # a real JPEG


def test_bytes_sent_counts_the_pictures(service_engine, user_id, figured):
    task = start_task(service_engine, user_id, figured)

    (row,) = tasks(service_engine)
    json_only = len(json.dumps(task.payload).encode("utf-8"))
    assert row["bytes_sent"] == json_only + len(task.pictures[0].data)


def test_the_paper_is_not_linked_until_transcribed(service_engine, user_id, paper1):
    start(service_engine, user_id, paper1)

    with service_engine.connect() as conn:
        linked = conn.execute(
            select(past_exam.c.transcript_task_id).where(past_exam.c.id == paper1)
        ).scalar_one()
    assert linked is None  # 1.10 sets it, when the items land (D-45)


def test_the_payload_never_carries_the_file(service_engine, user_id, paper1, tmp_path):
    payload = start(service_engine, user_id, paper1)

    with service_engine.connect() as conn:
        file_ref, sha = conn.execute(
            select(past_exam.c.file_ref, past_exam.c.content_sha256).where(past_exam.c.id == paper1)
        ).one()
    sent = json.dumps(payload)
    for secret in (file_ref, sha, str(tmp_path), "I3302_First.pdf"):
        assert secret not in sent


def test_the_courses_taggable_topics_travel_in_rules(service_engine, user_id, web, paper1):
    other = add_course(service_engine, user_id, "I3301", "Software Engineering", SEM)
    with write_unit(service_engine) as conn:
        for course_id, name, status in [
            (web["course_id"], "Regular expressions", "active"),
            (web["course_id"], "Sessions", "proposed"),
            (web["course_id"], "Old name", "declined"),
            (other["course_id"], "UML", "active"),
        ]:
            conn.execute(
                insert(topic).values(
                    id=new_id(),
                    user_id=user_id,
                    course_id=course_id,
                    name=name,
                    status=status,
                    proposed_by="profile",
                    status_changed_at=now(),
                    created_at=now(),
                )
            )

    payload = start(service_engine, user_id, paper1)

    topics = payload["rules"]["topics"]
    assert sorted((t["name"], t["status"]) for t in topics) == [
        ("Regular expressions", "active"),
        ("Sessions", "proposed"),
    ]
    assert all(set(t) == {"id", "name", "status"} for t in topics)


# --- the log line (build plan: one structured line per generation call) ----------


def test_each_call_logs_one_json_line(service_engine, user_id, figured, caplog):
    caplog.set_level("INFO", logger="studysystem")

    first = start_task(service_engine, user_id, figured)
    start_task(service_engine, user_id, figured)

    lines = [json.loads(r.getMessage()) for r in caplog.records]
    assert [line["resend"] for line in lines] == [False, True]
    assert lines[0] == {
        "event": "start_generation",
        "task_id": first.payload["task_id"],
        "kind": "transcription",
        "past_exam_id": figured,
        "resend": False,
        "pages": 3,
        "pictures": 1,
        "topics": 0,
        "bytes": measure(first.payload, first.pictures),
    }


def test_a_refusal_logs_nothing(service_engine, user_id, web, caplog):
    caplog.set_level("INFO", logger="studysystem")

    refused(service_engine, user_id, "transcription", "no-such-paper")

    assert caplog.records == []


# --- starting again (D-46) ------------------------------------------------------


def test_starting_again_hands_back_the_same_open_task(service_engine, user_id, paper1):
    first = start_task(service_engine, user_id, paper1)
    second = start_task(service_engine, user_id, paper1)

    assert second == first
    (row,) = tasks(service_engine)
    assert row["bytes_sent"] == 2 * measure(first.payload, first.pictures)  # every send counts


def test_another_paper_gets_its_own_task(service_engine, user_id, paper1, tmp_path):
    paper2 = register(
        service_engine, user_id, tmp_path / "in", "I3302_Second.pdf", pdf_bytes("S2"), "2020-09-14"
    )

    a = start(service_engine, user_id, paper1)
    b = start(service_engine, user_id, paper2)

    assert a["task_id"] != b["task_id"]
    assert task_count(service_engine) == 2


def test_a_transcribed_paper_is_refused(service_engine, user_id, paper1):
    task_id = start(service_engine, user_id, paper1)["task_id"]
    with write_unit(service_engine) as conn:  # what 1.10 will do when the items land
        conn.execute(
            update(generation_task)
            .where(generation_task.c.id == task_id)
            .values(status="closed", closed_at=now())
        )
        conn.execute(
            update(past_exam).where(past_exam.c.id == paper1).values(transcript_task_id=task_id)
        )

    err = refused(service_engine, user_id, "transcription", paper1)

    assert err.code == "already_transcribed"
    assert task_count(service_engine) == 1


# --- refusals write nothing ------------------------------------------------------


def test_another_students_paper_is_not_found(service_engine, user_id, other_user_id, web, tmp_path):
    add_course(service_engine, other_user_id, "I3302", "Server-Side Web Development", SEM)
    theirs = register(service_engine, other_user_id, tmp_path / "them", "p.pdf", pdf_bytes("x"))

    err = refused(service_engine, user_id, "transcription", theirs)

    assert err.code == "not_found"
    assert err.field_errors[0]["field"] == "past_exam_id"
    assert task_count(service_engine) == 0


def test_an_unknown_paper_is_not_found(service_engine, user_id, web):
    err = refused(service_engine, user_id, "transcription", new_id())

    assert err.code == "not_found"
    assert task_count(service_engine) == 0


def test_a_scan_waits_for_the_image_path(service_engine, user_id, web, tmp_path):
    scan = register(service_engine, user_id, tmp_path / "in", "scan.pdf", pdf_bytes(None))

    err = refused(service_engine, user_id, "transcription", scan)

    assert err.code == "not_supported_yet"
    assert task_count(service_engine) == 0


@pytest.mark.parametrize(
    ("kind", "code", "field"),
    [
        ("practice", "not_supported_yet", "kind"),  # a real kind, not built yet (D-47)
        ("quiz", "invalid_value", "kind"),  # not a kind at all
    ],
)
def test_kinds_other_than_transcription_are_refused(
    service_engine, user_id, paper1, kind, code, field
):
    err = refused(service_engine, user_id, kind, paper1)

    assert (err.code, err.field_errors[0]["field"]) == (code, field)
    assert task_count(service_engine) == 0


def test_transcription_needs_a_paper(service_engine, user_id, web):
    err = refused(service_engine, user_id, "transcription", None)

    assert (err.code, err.field_errors[0]["field"]) == ("invalid_value", "past_exam_id")


def test_kind_matches_in_any_case(service_engine, user_id, paper1):
    payload = start(service_engine, user_id, paper1, kind=" Transcription ")

    assert payload["task_id"] == tasks(service_engine)[0]["id"]


# --- the tool ------------------------------------------------------------------


def test_the_tool_sends_exactly_bytes_sent(call, service_engine, web, figured):
    result = call("study_start_generation", {"kind": "transcription", "past_exam_id": figured})

    assert not result.is_error, result.content[0].text
    text, *pictures = result.content
    assert [(b.type, b.mime_type) for b in pictures] == [("image", "image/jpeg")]
    (row,) = tasks(service_engine)
    sent = len(text.text.encode("utf-8")) + sum(len(b.data) for b in pictures)
    assert sent == row["bytes_sent"]
    assert json.loads(text.text)["task_id"] == row["id"]
