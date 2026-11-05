"""Redoing a chapter's note (D-250): `redo` starts a new note task on a chapter that has one. The
old note stays live until the new one is accepted, then retires with its row kept, and its files
are deleted so the new note takes the plain name.

Every test writes to a vault under tmp_path; conftest unsets the real one."""

import json
from pathlib import Path

import pytest
from sqlalchemy import select, update
from sqlalchemy.exc import SQLAlchemyError

from studysystem.db.tables import note
from studysystem.errors import StudyError
from studysystem.services import generation, notes
from studysystem.services.generation import start_generation, submit_generation
from studysystem.services.notes import export_course_notes
from studysystem.services.plan import _taught_materials
from studysystem.services.summaries import summary_task
from studysystem.services.units import write_unit
from tests.test_note_generation import (  # noqa: F401 - fixtures
    ch6,
    i3302,
    note_tasks,
    refused,
    set_status,
    start,
    web,
)
from tests.test_notes import (  # noqa: F401 - fixtures
    FOLDER,
    NOTE_FILE,
    PDF_FILE,
    declared,
    fake_pdf,
    submit_note,
    task_id,
    vault,
)

OLD_BODY = "# Old\n\n🎯 old target\n\n## To-Memorize\n\n| old table |\n"
NEW_BODY = "# New\n\n🎯 new target\n\n## To-Memorize\n\n| new table |\n"
MD = f"{FOLDER}/notes/md/{NOTE_FILE}"
PDF = f"{FOLDER}/notes/pdf/{PDF_FILE}"


def redo(engine, user_id, material_id):
    return start_generation(engine, user_id, "note", material_id=material_id, redo=True)


def rows(engine) -> list:
    with engine.connect() as conn:
        return conn.execute(select(note).order_by(note.c.created_at, note.c.id)).all()


@pytest.fixture
def noted(service_engine, user_id, task_id):  # noqa: F811
    """Chapter 6 with its first note saved, before any vault folder is declared."""
    submit_note(service_engine, user_id, task_id, OLD_BODY)


@pytest.fixture
def exported(service_engine, user_id, vault, declared, task_id):  # noqa: F811
    """Chapter 6 with its first note saved and its two files in the vault."""
    submit_note(service_engine, user_id, task_id, OLD_BODY)


# --- starting ---------------------------------------------------------------------------


def test_a_second_note_needs_redo(service_engine, user_id, ch6, noted):  # noqa: F811
    assert refused(service_engine, user_id, ch6).code == "already_noted"

    task = redo(service_engine, user_id, ch6)

    assert task.payload["content"]
    assert len(note_tasks(service_engine)) == 2


def test_redo_on_a_chapter_with_no_note_starts_as_usual(service_engine, user_id, ch6):  # noqa: F811
    assert redo(service_engine, user_id, ch6).payload["task_id"] == note_tasks(service_engine)[0].id


def test_an_open_redo_comes_back_as_itself(service_engine, user_id, ch6, noted):  # noqa: F811
    first = redo(service_engine, user_id, ch6)
    again = redo(service_engine, user_id, ch6)

    assert again.payload["task_id"] == first.payload["task_id"]
    assert len(note_tasks(service_engine)) == 2


def test_a_plain_start_never_takes_over_an_open_redo(service_engine, user_id, ch6, noted):  # noqa: F811
    """Without this, a host that only meant to read the task could replace the note."""
    redo(service_engine, user_id, ch6)

    assert refused(service_engine, user_id, ch6).code == "already_noted"
    assert [r.retired_at for r in rows(service_engine)] == [None]


def test_redo_refuses_an_open_task_that_kept_its_note(service_engine, user_id, ch6):  # noqa: F811
    """Handed back, it would skip the new text and report no error: section 1 is already on it."""
    first = start(service_engine, user_id, ch6).payload["task_id"]
    two_sections = {"notes": [{"section": 1, "body": OLD_BODY}, {"section": 2, "body": "#"}]}
    assert submit_generation(service_engine, user_id, first, two_sections)["status"] == "open"

    with pytest.raises(StudyError) as excinfo:
        redo(service_engine, user_id, ch6)
    assert (excinfo.value.code, excinfo.value.field_errors[0]["field"]) == (
        "note_task_open",
        "redo",
    )

    # its fix works: the resend closes the task, and the redo then starts
    assert submit_note(service_engine, user_id, first, OLD_BODY)["status"] == "closed"
    new_task = redo(service_engine, user_id, ch6).payload["task_id"]
    assert submit_note(service_engine, user_id, new_task, NEW_BODY)["accepted"] == [1]
    assert [r.body for r in rows(service_engine) if r.retired_at is None] == [NEW_BODY]


def test_a_plain_start_still_gets_an_open_task_that_kept_its_note(
    service_engine,
    user_id,
    ch6,  # noqa: F811
):
    """D-155 stands: a lost payload can be fetched."""
    first = start(service_engine, user_id, ch6).payload["task_id"]
    two_sections = {"notes": [{"section": 1, "body": OLD_BODY}, {"section": 2, "body": "#"}]}
    submit_generation(service_engine, user_id, first, two_sections)

    assert start(service_engine, user_id, ch6).payload["task_id"] == first


def test_an_invalidated_redo_tells_the_host_to_ask_for_the_redo_again(
    service_engine,
    user_id,
    ch6,  # noqa: F811
    noted,
):
    """A plain start would be refused `already_noted`, so that fix must not be the instruction."""
    new_task = redo(service_engine, user_id, ch6).payload["task_id"]
    set_status(service_engine, new_task, "invalidated")

    with pytest.raises(StudyError) as excinfo:
        submit_note(service_engine, user_id, new_task, NEW_BODY)

    assert excinfo.value.code == "task_closed"
    assert "redo=true" in excinfo.value.fix
    assert "old note stays in use" in excinfo.value.fix
    assert redo(service_engine, user_id, ch6).payload["task_id"] != new_task


@pytest.mark.parametrize("bad", ["banana", 1])
def test_a_bad_redo_value_gets_a_structured_error(service_engine, user_id, ch6, bad):  # noqa: F811
    with pytest.raises(StudyError) as excinfo:
        start_generation(service_engine, user_id, "note", material_id=ch6, redo=bad)

    assert excinfo.value.field_errors[0]["field"] == "redo"


def test_the_tool_takes_redo_as_cowork_sends_it(call, service_engine, ch6, noted):  # noqa: F811
    result = call("study_start_generation", {"kind": "note", "material_id": ch6, "redo": "true"})

    assert not result.is_error, result.content[0].text
    assert json.loads(result.content[0].text)["task_id"] == note_tasks(service_engine)[1].id


# --- retiring ---------------------------------------------------------------------------


def test_the_old_note_stays_live_until_the_new_one_lands(
    service_engine,
    user_id,
    ch6,  # noqa: F811
    noted,
):
    new_task = redo(service_engine, user_id, ch6).payload["task_id"]

    assert [r.retired_at for r in rows(service_engine)] == [None]

    submit_note(service_engine, user_id, new_task, NEW_BODY)

    old, new = rows(service_engine)
    assert old.retired_at is not None
    assert old.body == OLD_BODY  # the row and its text stay
    assert (new.retired_at, new.body) == (None, NEW_BODY)


def test_a_rejected_redo_leaves_the_old_note_live(service_engine, user_id, ch6, noted):  # noqa: F811
    new_task = redo(service_engine, user_id, ch6).payload["task_id"]

    reply = submit_generation(service_engine, user_id, new_task, {"notes": [{"section": 1}]})

    assert reply["status"] == "open"
    assert [r.retired_at for r in rows(service_engine)] == [None]


def test_a_failed_submit_rolls_the_retire_back(
    service_engine,
    user_id,
    ch6,  # noqa: F811
    noted,
    monkeypatch,
):
    """The retire and the new note share one write unit."""
    new_task = redo(service_engine, user_id, ch6).payload["task_id"]

    def fails(*args, **kwargs):
        raise RuntimeError("the unit fails after the retire")

    monkeypatch.setattr(generation, "_settle_task", fails)
    with pytest.raises(RuntimeError):
        submit_note(service_engine, user_id, new_task, NEW_BODY)

    assert [(r.body, r.retired_at) for r in rows(service_engine)] == [(OLD_BODY, None)]


def test_a_redone_chapter_is_already_noted_again(service_engine, user_id, ch6, noted):  # noqa: F811
    new_task = redo(service_engine, user_id, ch6).payload["task_id"]
    submit_note(service_engine, user_id, new_task, NEW_BODY)

    assert refused(service_engine, user_id, ch6).code == "already_noted"


# --- the readers ------------------------------------------------------------------------


def test_the_summary_task_reads_only_the_new_note(service_engine, user_id, ch6, noted):  # noqa: F811
    new_task = redo(service_engine, user_id, ch6).payload["task_id"]
    submit_note(service_engine, user_id, new_task, NEW_BODY)

    exam = summary_task(service_engine, user_id, ch6, 1).payload["exam"]

    assert exam["note_targets"] == ["new target"]
    assert "new table" in exam["note_memorize"]
    assert "old table" not in exam["note_memorize"]


def test_a_redone_chapter_still_counts_as_taught(service_engine, user_id, ch6, noted):  # noqa: F811
    new_task = redo(service_engine, user_id, ch6).payload["task_id"]
    submit_note(service_engine, user_id, new_task, NEW_BODY)

    with service_engine.connect() as conn:
        taught = conn.execute(_taught_materials(user_id)).scalars().all()

    assert ch6 in taught


# --- the files --------------------------------------------------------------------------


def test_the_old_files_go_and_the_new_note_takes_the_plain_name(
    service_engine,
    user_id,
    vault,  # noqa: F811
    ch6,  # noqa: F811
    exported,
):
    new_task = redo(service_engine, user_id, ch6).payload["task_id"]

    (export,) = submit_note(service_engine, user_id, new_task, NEW_BODY)["exports"]

    assert (export["exported_path"], export["pdf_path"], export["renamed"]) == (MD, PDF, False)
    text = (vault / MD).read_text(encoding="utf-8")
    assert NEW_BODY in text
    assert OLD_BODY not in text
    old, _ = rows(service_engine)
    assert (old.exported_path, old.pdf_path) == (None, None)
    assert old.body == OLD_BODY  # the database can write the old md again


def test_a_second_redo_also_lands_on_the_plain_name(
    service_engine,
    user_id,
    vault,  # noqa: F811
    ch6,  # noqa: F811
    exported,
):
    for body in (NEW_BODY, "# Third\n"):
        new_task = redo(service_engine, user_id, ch6).payload["task_id"]
        (export,) = submit_note(service_engine, user_id, new_task, body)["exports"]
        assert (export["exported_path"], export["renamed"]) == (MD, False)

    assert "# Third" in (vault / MD).read_text(encoding="utf-8")
    assert [r.body for r in rows(service_engine) if r.retired_at is None] == ["# Third\n"]


def test_a_blocked_delete_holds_the_new_files_until_the_retry(
    service_engine,
    user_id,
    vault,  # noqa: F811
    ch6,  # noqa: F811
    exported,
    monkeypatch,
):
    """A PDF open in another program can block the delete on Windows. The new note is safe, and
    its files wait, so the retry still gives it the plain name (D-250)."""
    new_task = redo(service_engine, user_id, ch6).payload["task_id"]

    def in_use(self, missing_ok=False):
        raise PermissionError(13, "the file is in use")

    with monkeypatch.context() as blocked:
        blocked.setattr(Path, "unlink", in_use)
        reply = submit_note(service_engine, user_id, new_task, NEW_BODY)

    (export,) = reply["exports"]
    assert export["exported_path"] is None
    assert "could not be deleted" in export["problem"]
    assert "study_export_notes" in export["fix"]
    assert reply["status"] == "closed"  # a failed file step never rolls back a note (D-72)
    old, new = rows(service_engine)
    assert old.retired_at is not None
    assert (old.exported_path, old.pdf_path) == (MD, PDF)  # still there, still recorded
    assert new.exported_path is None

    (export,) = export_course_notes(service_engine, user_id, "I3302")["exports"]

    assert (export["exported_path"], export["renamed"]) == (MD, False)
    assert NEW_BODY in (vault / MD).read_text(encoding="utf-8")


def test_a_delete_blocked_on_the_pdf_alone_keeps_each_path_apart(
    service_engine,
    user_id,
    vault,  # noqa: F811
    ch6,  # noqa: F811
    exported,
    monkeypatch,
):
    """The md goes and its path clears; the PDF stays recorded. The retry deletes only the PDF,
    and a later export touches nothing."""
    new_task = redo(service_engine, user_id, ch6).payload["task_id"]
    real_unlink = Path.unlink

    def pdf_in_use(self, missing_ok=False):
        if self.suffix == ".pdf":
            raise PermissionError(13, "the file is in use")
        real_unlink(self, missing_ok=missing_ok)

    with monkeypatch.context() as blocked:
        blocked.setattr(Path, "unlink", pdf_in_use)
        submit_note(service_engine, user_id, new_task, NEW_BODY)

    old, new = rows(service_engine)
    assert (old.exported_path, old.pdf_path) == (None, PDF)
    assert not (vault / MD).exists()
    assert (vault / PDF).exists()
    assert new.exported_path is None

    (export,) = export_course_notes(service_engine, user_id, "I3302")["exports"]

    assert (export["exported_path"], export["pdf_path"]) == (MD, PDF)
    assert export_course_notes(service_engine, user_id, "I3302")["exports"] == []
    assert NEW_BODY in (vault / MD).read_text(encoding="utf-8")
    assert (vault / PDF).exists()


def test_a_retired_path_a_live_note_now_holds_is_never_deleted(
    service_engine,
    user_id,
    vault,  # noqa: F811
    ch6,  # noqa: F811
    exported,
):
    """A failed save can leave a retired row naming a file a live note took since. Only the
    retired row's path clears, and the live note keeps its file."""
    new_task = redo(service_engine, user_id, ch6).payload["task_id"]
    submit_note(service_engine, user_id, new_task, NEW_BODY)
    with write_unit(service_engine) as conn:
        conn.execute(update(note).where(note.c.retired_at.is_not(None)).values(exported_path=MD))

    assert export_course_notes(service_engine, user_id, "I3302")["exports"] == []

    assert NEW_BODY in (vault / MD).read_text(encoding="utf-8")
    old, new = rows(service_engine)
    assert (old.exported_path, new.exported_path) == (None, MD)


def test_a_retired_path_differing_only_by_case_is_the_same_file(
    service_engine,
    user_id,
    vault,  # noqa: F811
    ch6,  # noqa: F811
    exported,
):
    """Windows treats the two names as one file, so the guard must too."""
    new_task = redo(service_engine, user_id, ch6).payload["task_id"]
    submit_note(service_engine, user_id, new_task, NEW_BODY)
    with write_unit(service_engine) as conn:
        conn.execute(
            update(note).where(note.c.retired_at.is_not(None)).values(exported_path=MD.lower())
        )

    export_course_notes(service_engine, user_id, "I3302")

    assert NEW_BODY in (vault / MD).read_text(encoding="utf-8")


def test_a_wrong_vault_path_deletes_nothing_and_loses_no_path(
    service_engine,
    user_id,
    vault,  # noqa: F811
    ch6,  # noqa: F811
    exported,
    monkeypatch,
    tmp_path,
):
    """The folder not being there must not read as "the old file is already gone" (D-250)."""
    new_task = redo(service_engine, user_id, ch6).payload["task_id"]

    with monkeypatch.context() as wrong:
        wrong.setenv("STUDYSYSTEM_VAULT_DIR", str(tmp_path / "not-the-vault"))
        (export,) = submit_note(service_engine, user_id, new_task, NEW_BODY)["exports"]

    assert export["exported_path"] is None
    assert "not a folder" in export["problem"]
    old, _ = rows(service_engine)
    assert (old.exported_path, old.pdf_path) == (MD, PDF)
    assert OLD_BODY in (vault / MD).read_text(encoding="utf-8")

    (export,) = export_course_notes(service_engine, user_id, "I3302")["exports"]

    assert (export["exported_path"], export["renamed"]) == (MD, False)


def test_a_failed_save_after_a_delete_says_so_and_heals_on_retry(
    service_engine,
    user_id,
    vault,  # noqa: F811
    ch6,  # noqa: F811
    exported,
    monkeypatch,
):
    new_task = redo(service_engine, user_id, ch6).payload["task_id"]

    def fails(*args, **kwargs):
        raise SQLAlchemyError("the database is busy")

    with monkeypatch.context() as busy:
        busy.setattr(notes, "_record", fails)
        (export,) = submit_note(service_engine, user_id, new_task, NEW_BODY)["exports"]

    assert "could not be saved" in export["problem"]
    assert not (vault / PDF).exists()

    (export,) = export_course_notes(service_engine, user_id, "I3302")["exports"]

    assert (export["exported_path"], export["renamed"]) == (MD, False)
    old, _ = rows(service_engine)
    assert (old.exported_path, old.pdf_path) == (None, None)


def test_an_old_file_already_gone_lets_the_new_note_go_out(
    service_engine,
    user_id,
    vault,  # noqa: F811
    ch6,  # noqa: F811
    exported,
):
    (vault / PDF).unlink()
    new_task = redo(service_engine, user_id, ch6).payload["task_id"]

    (export,) = submit_note(service_engine, user_id, new_task, NEW_BODY)["exports"]

    assert (export["exported_path"], export["pdf_path"]) == (MD, PDF)
    old, _ = rows(service_engine)
    assert (old.exported_path, old.pdf_path) == (None, None)


def test_a_retired_note_is_never_exported_again(
    service_engine,
    user_id,
    vault,  # noqa: F811
    ch6,  # noqa: F811
    exported,
):
    new_task = redo(service_engine, user_id, ch6).payload["task_id"]
    submit_note(service_engine, user_id, new_task, NEW_BODY)

    assert export_course_notes(service_engine, user_id, "I3302")["exports"] == []
    old, _ = rows(service_engine)
    assert (old.exported_path, old.pdf_path) == (None, None)
