"""Material intake (task 1.14): Chapter 6 goes in once - the same bytes again hand back the same
row, a new version of the chapter is a new row - and every refusal writes nothing."""

import json
from pathlib import Path

import pytest
from sqlalchemy import func, select

from studysystem.db.tables import material
from studysystem.errors import StudyError
from studysystem.server import create_server
from studysystem.services import intake
from studysystem.services.courses import add_course
from studysystem.services.materials import add_material
from tests._client import call_tool
from tests._papers import JPEG, pdf_bytes, write

SEM = "Semester 1 2026-2027"
CH6 = "PHP_Chapter6_Eng_DataBase.pdf"


@pytest.fixture
def web(service_engine, user_id):
    return add_course(service_engine, user_id, "I3302", "Server-side WEB Development", SEM)


@pytest.fixture
def ch6(tmp_path):
    """Chapter 6 as the host would point at it - a PDF with a text layer."""
    return write(tmp_path / "Course Notes", CH6, pdf_bytes("PHP and MySQL: mysqli_connect"))


def add(engine, user_id, path, **overrides):
    args = {"code": "I3302", "kind": "chapter"} | overrides
    return add_material(engine, user_id, path=path, **args)


def rows(engine):
    with engine.connect() as conn:
        return [r._mapping for r in conn.execute(select(material).order_by(material.c.id))]


def count(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(material)).scalar_one()


def kept() -> list[str]:
    folder = intake.papers_dir()
    return sorted(p.name for p in folder.iterdir()) if folder.exists() else []


# --- registering ------------------------------------------------------------


def test_chapter_6_is_one_row_with_its_file_kept_by_hash(service_engine, user_id, web, ch6):
    out = add(service_engine, user_id, ch6)

    (row,) = rows(service_engine)
    assert out == {
        "material_id": row["id"],
        "course_id": web["course_id"],
        "filename": CH6,
        "kind": "chapter",
        "has_text_layer": True,
        "existing": False,
    }
    assert (row["user_id"], row["media_type"], row["has_text_layer"]) == (
        user_id,
        "application/pdf",
        1,
    )
    assert row["file_ref"] == f"{row['content_sha256']}.pdf"
    assert kept() == [row["file_ref"]]


def test_the_kind_matches_in_any_case(service_engine, user_id, web, ch6):
    assert add(service_engine, user_id, ch6, kind=" Chapter ")["kind"] == "chapter"


def test_a_scanned_pdf_is_accepted_with_no_text_layer(service_engine, user_id, web, tmp_path):
    """The note task refuses it later, as transcription refuses a scanned paper."""
    scan = write(tmp_path, "ch6-scan.pdf", pdf_bytes(None))

    assert add(service_engine, user_id, scan)["has_text_layer"] is False
    assert rows(service_engine)[0]["has_text_layer"] == 0


# --- the same bytes are the same material -----------------------------------


def test_the_same_pdf_again_hands_back_the_row_and_writes_nothing(
    service_engine, user_id, web, ch6
):
    first = add(service_engine, user_id, ch6)

    again = add(service_engine, user_id, ch6)

    assert again == {**first, "existing": True}
    assert count(service_engine) == 1


def test_a_renamed_copy_is_the_same_material_under_its_first_name(
    service_engine, user_id, web, ch6, tmp_path
):
    """`ch6.pdf` with Chapter 6's bytes: the hash catches it, a filename check would not."""
    first = add(service_engine, user_id, ch6)
    copy = write(tmp_path / "elsewhere", "ch6.pdf", Path(ch6).read_bytes())

    again = add(service_engine, user_id, copy, kind="slides")

    assert again == {**first, "existing": True}  # its first name and kind, unchanged
    assert count(service_engine) == 1


def test_a_new_version_with_the_same_name_is_a_new_material(
    service_engine, user_id, web, ch6, tmp_path
):
    """D-73: next year's Chapter 6 - same filename, new bytes."""
    add(service_engine, user_id, ch6)
    v2 = write(tmp_path / "2027", CH6, pdf_bytes("PHP and MySQL: PDO this year"))

    out = add(service_engine, user_id, v2)

    assert out["existing"] is False
    assert [r["filename"] for r in rows(service_engine)] == [CH6, CH6]
    assert len(kept()) == 2


def test_the_same_pdf_under_another_course_is_its_own_row(service_engine, user_id, web, ch6):
    add_course(service_engine, user_id, "I3306", "Database II", SEM)
    add(service_engine, user_id, ch6)

    assert add(service_engine, user_id, ch6, code="I3306")["existing"] is False
    assert count(service_engine) == 2
    assert len(kept()) == 1  # one copy serves both


# --- refusals: each writes nothing -------------------------------------------


def refused(engine, user_id, path, **overrides) -> StudyError:
    with pytest.raises(StudyError) as excinfo:
        add(engine, user_id, path, **overrides)
    return excinfo.value


def test_page_photos_are_not_supported_yet(service_engine, user_id, web, tmp_path):
    photo = write(tmp_path, "IMG_1.jpg", JPEG)

    err = refused(service_engine, user_id, photo)

    assert (err.code, err.field_errors[0]["field"]) == ("not_supported_yet", "path")
    assert count(service_engine) == 0 and kept() == []


@pytest.mark.parametrize(
    ("make", "field"),
    [
        (lambda d: write(d, "ch6.docx", b"PK\x03\x04"), "path"),  # not a PDF
        (lambda d: write(d, "ch6.pdf", b"not a pdf at all"), "path"),  # unreadable PDF
        (lambda d: str(d / "nope.pdf"), "path"),  # no such file
        (lambda d: "   ", "path"),
    ],
)
def test_a_bad_file_is_invalid_on_path_and_writes_nothing(
    service_engine, user_id, web, tmp_path, make, field
):
    err = refused(service_engine, user_id, make(tmp_path))

    assert (err.code, err.field_errors[0]["field"]) == ("invalid_value", field)
    assert count(service_engine) == 0 and kept() == []


def test_a_bad_kind_is_invalid_and_writes_nothing(service_engine, user_id, web, ch6):
    err = refused(service_engine, user_id, ch6, kind="lecture")

    assert (err.code, err.field_errors[0]["field"]) == ("invalid_value", "kind")
    assert "chapter" in err.fix
    assert count(service_engine) == 0 and kept() == []


def test_an_unknown_course_is_not_found_and_nothing_is_kept(service_engine, user_id, web, ch6):
    err = refused(service_engine, user_id, ch6, code="I3399")

    assert err.code == "not_found"
    assert count(service_engine) == 0 and kept() == []


# --- another student (Conventions: ownership) ---------------------------------


def test_the_same_pdf_from_another_student_is_their_own_row(
    service_engine, user_id, other_user_id, web, ch6
):
    """B's own I3302, the same Chapter 6 bytes: a new row for B, never A's material_id."""
    mine = add(service_engine, user_id, ch6)
    b_web = add_course(service_engine, other_user_id, "I3302", "Server-side WEB Development", SEM)

    theirs = add(service_engine, other_user_id, ch6)

    assert theirs["existing"] is False
    assert theirs["material_id"] != mine["material_id"]
    assert theirs["course_id"] == b_web["course_id"]
    assert {(r["user_id"], r["course_id"]) for r in rows(service_engine)} == {
        (user_id, web["course_id"]),
        (other_user_id, b_web["course_id"]),
    }


# --- through the tool, as the host calls it ------------------------------------


def test_chapter_6_through_the_tool(call, service_engine, web, ch6):
    first = call("study_add_material", {"code": "I3302", "path": str(ch6), "kind": "chapter"})
    again = call("study_add_material", {"code": "I3302", "path": str(ch6), "kind": "chapter"})

    assert not first.is_error
    (row,) = rows(service_engine)
    assert first.structured_content["material_id"] == row["id"]
    assert first.structured_content["existing"] is False
    assert again.structured_content == {**first.structured_content, "existing": True}


def test_a_refusal_through_the_tool_is_the_whole_error(call, service_engine, web, ch6):
    result = call("study_add_material", {"code": "I3399", "path": str(ch6), "kind": "chapter"})

    assert result.is_error
    body = json.loads(result.content[0].text)
    assert body["code"] == "not_found"
    assert body["fix"]
    assert result.structured_content == body
    assert count(service_engine) == 0
    assert kept() == []


def tool_on(engine):
    """`call` without its user: a server on a database `study migrate` never gave one."""
    server = create_server(engine)

    def _call(name: str, args: dict):
        return call_tool(server, name, args)

    return _call


def test_a_database_with_no_user_answers_no_user_through_the_tool(service_engine, ch6):
    host = tool_on(service_engine)

    result = host("study_add_material", {"code": "I3302", "path": str(ch6), "kind": "chapter"})

    assert result.is_error
    assert result.structured_content["code"] == "no_user"
    assert result.structured_content["fix"] == "run: study migrate"
    assert count(service_engine) == 0
    assert kept() == []  # refused before the file was read, let alone kept


def test_a_database_with_two_users_answers_too_many_users_through_the_tool(
    call, service_engine, other_user_id, ch6
):
    result = call("study_add_material", {"code": "I3302", "path": str(ch6), "kind": "chapter"})

    assert result.is_error
    assert result.structured_content["code"] == "too_many_users"
    assert "snapshots" in result.structured_content["fix"]
    assert count(service_engine) == 0
    assert kept() == []
