"""The note export (1.14; D-10, D-72, D-73): after the submit commits, Chapter 6's note is copied
into the course's `notes/` folder with the vault's stamp. A file is never overwritten, a typo in
the folder creates nothing, and no problem raises - the note is already saved.

Every test writes to a vault under tmp_path; conftest unsets the real one."""

import pytest
from sqlalchemy import select, update

from studysystem.db.tables import note
from studysystem.services.courses import set_course_input
from studysystem.services.generation import submit_generation
from studysystem.services.notes import _free_name, _note_name, _stamp, export_notes
from studysystem.services.units import write_unit
from tests.test_note_generation import ch6, i3302, start, web  # noqa: F401 - fixtures

FOLDER = "uni/Semester 1 2026-2027/Server-Side Web Development"
NOTE_FILE = "PHP_Chapter6_Eng_DataBase_notes.md"
BODY = "# PHP & MySQL\n\n🎯 prepared statements - Final 2020-02-17 Q4\n"


@pytest.fixture
def vault(tmp_path, monkeypatch):
    """The vault root with I3302's course folder in it - no notes/ yet, as in the real vault
    before its first note."""
    root = tmp_path / "vault"
    (root / FOLDER).mkdir(parents=True)
    monkeypatch.setenv("STUDYSYSTEM_VAULT_DIR", str(root))
    return root


@pytest.fixture
def declared(service_engine, user_id, i3302):  # noqa: F811
    set_course_input(service_engine, user_id, "I3302", "vault_folder", FOLDER)


@pytest.fixture
def task_id(service_engine, user_id, ch6):  # noqa: F811
    return start(service_engine, user_id, ch6).payload["task_id"]


def submit_note(engine, user_id, task_id, body=BODY) -> dict:
    return submit_generation(engine, user_id, task_id, {"notes": [{"section": 1, "body": body}]})


def exported_path(engine):
    with engine.connect() as conn:
        return conn.execute(select(note.c.exported_path)).scalar_one()


# --- written ------------------------------------------------------------------------


def test_chapter_6_s_note_lands_in_notes_with_its_stamp(
    service_engine, user_id, vault, declared, task_id
):
    reply = submit_note(service_engine, user_id, task_id)

    relative = f"{FOLDER}/notes/{NOTE_FILE}"
    assert reply["exports"] == [{"section": 1, "exported_path": relative, "renamed": False}]
    assert exported_path(service_engine) == relative  # relative, with / (D-72)
    text = (vault / relative).read_text(encoding="utf-8")
    assert text == _stamp(1, task_id, "PHP_Chapter6_Eng_DataBase.pdf", 1) + BODY


def test_the_file_is_utf_8_so_the_markers_survive(
    service_engine, user_id, vault, declared, task_id
):
    """Windows' default codec cannot write 🎯 - the export names utf-8 itself."""
    submit_note(service_engine, user_id, task_id)

    raw = (vault / FOLDER / "notes" / NOTE_FILE).read_bytes()
    assert "🎯".encode() in raw


def test_an_existing_note_is_never_overwritten(service_engine, user_id, vault, declared, task_id):
    """Last year's Chapter 6 note, with his annotations, already holds the name (D-73)."""
    notes_dir = vault / FOLDER / "notes"
    notes_dir.mkdir()
    (notes_dir / NOTE_FILE).write_text("my annotations", encoding="utf-8")

    reply = submit_note(service_engine, user_id, task_id)

    (export,) = reply["exports"]
    assert export["exported_path"].endswith("PHP_Chapter6_Eng_DataBase_notes (2).md")
    assert export["renamed"] is True
    assert (notes_dir / NOTE_FILE).read_text(encoding="utf-8") == "my annotations"


def test_an_exported_note_is_not_exported_again(service_engine, user_id, vault, declared, task_id):
    submit_note(service_engine, user_id, task_id)

    assert export_notes(service_engine, user_id, task_id) == []
    assert len(list((vault / FOLDER / "notes").iterdir())) == 1


# --- not written: the note is saved anyway ---------------------------------------------


def test_no_vault_root_names_the_env_var(service_engine, user_id, declared, task_id):
    reply = submit_note(service_engine, user_id, task_id)

    (export,) = reply["exports"]
    assert export["exported_path"] is None
    assert "STUDYSYSTEM_VAULT_DIR" in export["fix"]
    assert reply["status"] == "closed"  # the note is saved all the same
    assert exported_path(service_engine) is None


def test_no_vault_folder_names_the_tool(service_engine, user_id, vault, i3302, task_id):  # noqa: F811
    reply = submit_note(service_engine, user_id, task_id)

    (export,) = reply["exports"]
    assert export["exported_path"] is None
    assert "study_set_course_input" in export["fix"]
    assert not (vault / FOLDER / "notes").exists()


def test_a_typo_in_the_folder_creates_nothing(service_engine, user_id, vault, i3302, task_id):  # noqa: F811
    typo = "uni/Semester 1 2026-2027/Server-Side Web Developmnet"
    set_course_input(service_engine, user_id, "I3302", "vault_folder", typo)

    reply = submit_note(service_engine, user_id, task_id)

    (export,) = reply["exports"]
    assert export["exported_path"] is None
    assert typo in export["problem"]
    assert not (vault / typo).exists()  # no fake course folder grown in the vault


def test_a_write_failure_is_reported_not_raised(service_engine, user_id, vault, declared, task_id):
    """notes is a file, not a folder: the OS refuses, the submit still answers."""
    (vault / FOLDER / "notes").write_text("not a folder", encoding="utf-8")

    reply = submit_note(service_engine, user_id, task_id)

    (export,) = reply["exports"]
    assert export["exported_path"] is None
    assert "could not be written" in export["problem"]
    assert reply["status"] == "closed"


def test_an_unexported_note_goes_out_on_the_next_export(
    service_engine, user_id, vault, declared, task_id
):
    submit_note(service_engine, user_id, task_id)
    with write_unit(service_engine) as conn:  # as if the first export had failed
        conn.execute(update(note).values(exported_path=None))
    (vault / FOLDER / "notes" / NOTE_FILE).unlink()

    (export,) = export_notes(service_engine, user_id, task_id)

    assert export["exported_path"] == f"{FOLDER}/notes/{NOTE_FILE}"


# --- names --------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("section", "name"),
    [
        (1, "PHP_Chapter6_Eng_DataBase_notes"),
        (2, "PHP_Chapter6_Eng_DataBase_notes - part 2"),  # never (2): that marks a clash
    ],
)
def test_a_note_is_named_after_its_material(section, name):
    assert _note_name("PHP_Chapter6_Eng_DataBase.pdf", section) == name


def test_the_free_name_counts_past_every_taken_one(tmp_path):
    stem = "PHP_Chapter6_Eng_DataBase_notes"
    assert _free_name(tmp_path, stem) == tmp_path / f"{stem}.md"
    (tmp_path / f"{stem}.md").touch()
    assert _free_name(tmp_path, stem) == tmp_path / f"{stem} (2).md"
    (tmp_path / f"{stem} (2).md").touch()
    assert _free_name(tmp_path, stem) == tmp_path / f"{stem} (3).md"


# --- the stamp -----------------------------------------------------------------------------


def test_the_stamp_uses_the_vault_s_keys():
    stamp = _stamp(1, "01M3TASK0000000000000000AA", "PHP_Chapter6_Eng_DataBase.pdf", 1)

    assert stamp.splitlines() == [
        "---",
        "tags: [study-system, note]",
        "type: note",
        "generated: true",
        "profile-version: 1",
        'source-task: "01M3TASK0000000000000000AA"',
        'material: "PHP_Chapter6_Eng_DataBase.pdf"',
        "section: 1",
        "---",
    ]
    assert stamp.endswith("---\n")  # the body starts on its own line


def test_a_course_with_no_profile_stamps_null():
    assert "profile-version: null" in _stamp(None, "01M3TASK0000000000000000AA", "ch.pdf", 1)


def test_a_filename_with_a_colon_stays_one_value():
    stamp = _stamp(1, "01M3TASK0000000000000000AA", "Ch 6: MySQL #1.pdf", 1)
    assert 'material: "Ch 6: MySQL #1.pdf"' in stamp


def test_a_failed_read_is_reported_not_raised(service_engine, user_id, monkeypatch):
    """The note is committed before the export runs, so even the export's first read failing
    must come back as an entry, not as a failed submit."""
    from sqlalchemy.exc import OperationalError

    from studysystem.services import notes

    def broken(*args):
        raise OperationalError("SELECT", {}, Exception("database is locked"))

    monkeypatch.setattr(notes, "_unexported_notes", broken)

    (export,) = export_notes(service_engine, user_id, "01M3TASK0000000000000000AA")

    assert (export["section"], export["exported_path"]) == (None, None)
    assert "could not be read" in export["problem"]
