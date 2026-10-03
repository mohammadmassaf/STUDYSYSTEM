"""The note task, outbound (1.14; D-46, D-70 - D-74): Chapter 6 leaves the server as its pages,
aimed by I3302's profile - the weights and the 24 past-exam items behind them. A material gets one
note: an open task comes back as itself, a closed one refuses, and every refusal writes nothing."""

import json

import pytest
from sqlalchemy import func, select, update

from studysystem.db.tables import generation_task
from studysystem.errors import StudyError
from studysystem.services.courses import add_course
from studysystem.services.generation import (
    NOTE_SCHEMA,
    NOTE_STEPS,
    measure,
    start_generation,
    submit_generation,
)
from studysystem.services.materials import add_material
from studysystem.services.profiles import derive_profile
from studysystem.services.units import write_unit
from tests._papers import pdf_bytes, pdf_pages, write
from tests.test_profiles import FINAL, I3302_PAPERS, NAMES, SEM, accept_all, add_paper, register

CH6_PAGES = ["PHP and MySQL", "mysqli_connect and prepared statements", "fetching rows"]


@pytest.fixture
def web(service_engine, user_id):
    return add_course(service_engine, user_id, "I3302", "Server-Side Web Development", SEM)


@pytest.fixture
def i3302(service_engine, user_id, web, tmp_path):
    """The state after 1.13: three papers transcribed, every topic accepted, profile v1."""
    for date, session_type, items in I3302_PAPERS:
        add_paper(service_engine, user_id, tmp_path, date, session_type, items)
    accept_all(service_engine, user_id)
    v1 = derive_profile(service_engine, user_id, "I3302", FINAL)
    return {**web, "profile_id": v1["exam_profile_id"]}


def material(engine, user_id, folder, name="PHP_Chapter6_Eng_DataBase.pdf", data=None) -> str:
    path = write(folder, name, data if data is not None else pdf_pages(CH6_PAGES))
    return add_material(engine, user_id, "I3302", path, "chapter")["material_id"]


@pytest.fixture
def ch6(service_engine, user_id, i3302, tmp_path):
    return material(service_engine, user_id, tmp_path / "Course Notes")


def start(engine, user_id, material_id):
    return start_generation(engine, user_id, "note", material_id=material_id)


def refused(engine, user_id, material_id) -> StudyError:
    with pytest.raises(StudyError) as excinfo:
        start(engine, user_id, material_id)
    return excinfo.value


def note_tasks(engine):
    with engine.connect() as conn:
        return conn.execute(
            select(generation_task)
            .where(generation_task.c.kind == "note")
            .order_by(generation_task.c.id)
        ).all()


def set_status(engine, task_id, status) -> None:
    """End the task directly - shorter than the submissions that would end it."""
    with write_unit(engine) as conn:
        conn.execute(
            update(generation_task)
            .where(generation_task.c.id == task_id)
            .values(status=status, closed_at="2026-10-01T20:00:00Z")
        )


# --- the task -----------------------------------------------------------------------


def test_chapter_6_leaves_as_its_pages_with_the_schema_and_steps(service_engine, user_id, ch6):
    payload = start(service_engine, user_id, ch6).payload

    assert [p["text"] for p in payload["content"]] == CH6_PAGES
    assert payload["schema"] == NOTE_SCHEMA
    assert payload["rules"]["steps"] == NOTE_STEPS


def test_one_task_row_records_it_on_profile_v1(service_engine, user_id, i3302, ch6):
    task = start(service_engine, user_id, ch6)

    (row,) = note_tasks(service_engine)
    assert row.id == task.payload["task_id"]
    assert (row.user_id, row.scope_type, row.scope_id, row.route, row.status) == (
        user_id,
        "material",
        ch6,
        "host",
        "open",
    )
    assert row.exam_profile_id == i3302["profile_id"]  # D-71
    assert row.bytes_sent == measure(task.payload, task.pictures)


def test_the_weights_travel_heaviest_first(service_engine, user_id, ch6):
    topics = start(service_engine, user_id, ch6).payload["rules"]["topics"]

    assert [(t["name"], round(t["weight"], 1)) for t in topics] == [
        (NAMES["files"], 25.0),
        (NAMES["mysql"], 23.2),
        (NAMES["sessions"], 17.2),
        (NAMES["forms"], 14.4),
        (NAMES["cookies"], 10.7),
        (NAMES["regex"], 9.5),
    ]
    assert all(len(t["id"]) == 26 for t in topics)


def test_the_24_past_exam_items_travel_for_e1_citations(service_engine, user_id, ch6):
    """D-74: the questions behind the weights, oldest paper first, in paper order."""
    items = start(service_engine, user_id, ch6).payload["rules"]["past_exam_items"]

    assert len(items) == 24
    assert items[0] == {
        "paper": "Final exam 2020-02-17 (first)",
        "position": 1,
        "marks": None,
        "topic": NAMES["regex"],
        "question": "Question 1",
    }
    assert items[3]["marks"] == 5.0 and items[3]["topic"] == NAMES["mysql"]
    assert items[-1]["paper"] == "Final exam 2021-09-20 (second)"


def test_only_the_profiles_papers_send_items(service_engine, user_id, i3302, ch6, tmp_path):
    """The profile's paper set (D-69): an untranscribed paper in its slot sends nothing."""
    register(service_engine, user_id, tmp_path, "2022-01-20")  # registered, never transcribed

    items = start(service_engine, user_id, ch6).payload["rules"]["past_exam_items"]

    assert len(items) == 24


def test_each_call_logs_one_json_line(service_engine, user_id, ch6, caplog):
    caplog.set_level("INFO", logger="studysystem.services.generation")

    start(service_engine, user_id, ch6)

    (line,) = [json.loads(r.message) for r in caplog.records if "start_generation" in r.message]
    assert (line["kind"], line["material_id"], line["resend"]) == ("note", ch6, False)
    assert (line["pages"], line["topics"], line["past_exam_items"]) == (3, 6, 24)


# --- one note per material (D-46, D-73) ----------------------------------------------


def test_starting_again_hands_back_the_same_open_task(service_engine, user_id, ch6):
    first = start(service_engine, user_id, ch6)
    again = start(service_engine, user_id, ch6)

    (row,) = note_tasks(service_engine)
    assert again.payload == first.payload
    assert row.bytes_sent == 2 * measure(first.payload, first.pictures)


def test_a_material_with_its_note_is_already_noted(service_engine, user_id, ch6):
    task_id = start(service_engine, user_id, ch6).payload["task_id"]
    submit_generation(service_engine, user_id, task_id, {"notes": [{"section": 1, "body": "#"}]})

    err = refused(service_engine, user_id, ch6)

    assert (err.code, err.field_errors[0]["field"]) == ("already_noted", "material_id")
    assert len(note_tasks(service_engine)) == 1


@pytest.mark.parametrize("ended", ["partial", "invalidated"])
def test_a_task_that_kept_a_note_still_blocks_a_second(service_engine, user_id, ch6, ended):
    """QA's path: section 1 lands, section 2 errs, so the task stays open holding a note - then
    ends partial or invalidated. The note row, not the task's status, decides (D-73, D-76)."""
    task_id = start(service_engine, user_id, ch6).payload["task_id"]
    reply = submit_generation(
        service_engine,
        user_id,
        task_id,
        {"notes": [{"section": 1, "body": "#"}, {"section": 2, "body": "#"}]},
    )
    assert (reply["accepted"], reply["status"]) == ([1], "open")
    set_status(service_engine, task_id, ended)

    assert refused(service_engine, user_id, ch6).code == "already_noted"


@pytest.mark.parametrize("ended", ["partial", "invalidated"])
def test_a_task_that_kept_no_note_lets_the_material_start_again(
    service_engine, user_id, ch6, ended
):
    old = start(service_engine, user_id, ch6).payload["task_id"]
    set_status(service_engine, old, ended)

    new = start(service_engine, user_id, ch6).payload["task_id"]

    assert new != old
    assert [t.status for t in note_tasks(service_engine)] == [ended, "open"]


def test_a_v2_derive_invalidates_the_task_and_the_next_one_carries_v2(service_engine, user_id, ch6):
    old = start(service_engine, user_id, ch6).payload["task_id"]
    v2 = derive_profile(service_engine, user_id, "I3302", FINAL)

    new = start(service_engine, user_id, ch6).payload["task_id"]

    old_row, new_row = note_tasks(service_engine)
    assert (old_row.id, old_row.status) == (old, "invalidated")
    assert (new_row.id, new_row.exam_profile_id) == (new, v2["exam_profile_id"])


def test_another_chapter_gets_its_own_task(service_engine, user_id, ch6, tmp_path):
    ch8 = material(
        service_engine,
        user_id,
        tmp_path / "Course Notes",
        "PHP_Chapter8_Eng_Files.pdf",
        pdf_pages(["PHP files: fopen, fwrite"]),
    )

    start(service_engine, user_id, ch6)
    start(service_engine, user_id, ch8)

    assert {t.scope_id for t in note_tasks(service_engine)} == {ch6, ch8}


# --- a course with no profile (D-71) -------------------------------------------------


def test_a_cold_start_course_gets_no_profile_and_no_evidence(
    service_engine, user_id, web, tmp_path
):
    material_id = material(service_engine, user_id, tmp_path)

    rules = start(service_engine, user_id, material_id).payload["rules"]

    (row,) = note_tasks(service_engine)
    assert row.exam_profile_id is None
    assert (rules["topics"], rules["past_exam_items"]) == ([], [])


# --- refusals: each writes nothing ----------------------------------------------------


def test_a_note_needs_a_material(service_engine, user_id, web):
    err = refused(service_engine, user_id, None)

    assert (err.code, err.field_errors[0]["field"]) == ("invalid_value", "material_id")


def test_an_unknown_material_is_not_found(service_engine, user_id, web):
    err = refused(service_engine, user_id, "01M3NOPE00000000000000000Z")

    assert (err.code, err.field_errors[0]["field"]) == ("not_found", "material_id")
    assert note_tasks(service_engine) == []


def test_another_students_material_is_not_found(service_engine, user_id, other_user_id, ch6):
    err = refused(service_engine, other_user_id, ch6)

    assert err.code == "not_found"
    assert note_tasks(service_engine) == []


def test_a_scanned_material_waits_for_the_image_path(service_engine, user_id, web, tmp_path):
    scan = material(service_engine, user_id, tmp_path, "ch6-scan.pdf", pdf_bytes(None))

    err = refused(service_engine, user_id, scan)

    assert (err.code, err.field_errors[0]["field"]) == ("not_supported_yet", "material_id")
    assert note_tasks(service_engine) == []


# --- the tool ---------------------------------------------------------------------------


def test_the_tool_starts_a_note_task(call, service_engine, ch6):
    result = call("study_start_generation", {"kind": "note", "material_id": ch6})

    assert not result.is_error, result.content[0].text
    payload = json.loads(result.content[0].text)
    assert payload["task_id"] == note_tasks(service_engine)[0].id
    with service_engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(generation_task)).scalar_one() == 4
