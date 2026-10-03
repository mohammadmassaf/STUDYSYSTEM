"""Course material (D-05, D-44, task 1.14): a chapter or slide deck comes in by local path, is kept
as the server's own copy named by its hash, and is known everywhere else by `material_id`.

One PDF per call. Page photos are refused for now: a set of photos has no single filename or
media type, and with no text layer a note task would refuse it anyway. A scanned PDF is
accepted - its `has_text_layer = 0`, and the note task is what refuses it, as for papers.

The same bytes under the same course are the same material: adding them again hands back the
existing row with `existing: true` and writes nothing. That is the table's unique key
`(course_id, content_sha256)`, so a renamed copy is caught, and a new version of a chapter -
same name, new bytes - is a new material (D-73).
"""

from pathlib import Path

from sqlalchemy import Engine, insert, select

from studysystem.db.tables import material
from studysystem.errors import StudyError
from studysystem.services import intake, values
from studysystem.services.ids import new_id, now
from studysystem.services.lookups import find_course
from studysystem.services.units import write_unit

KINDS = ("slides", "chapter", "lecture-notes", "textbook", "exercises", "syllabus", "other")


def add_material(
    engine: Engine,
    user_id: str,
    code: str,
    path: str,
    kind: str,
    semester_name: str | None = None,
) -> dict:
    """Register one PDF as course material, or hand back the row that already holds its bytes.
    Returns {"material_id", "course_id", "filename", "kind", "has_text_layer", "existing"}."""

    kind = values.choice("kind", kind, KINDS)
    path = values.text("path", path)
    suffix = Path(path).suffix.lower()
    if suffix in intake.IMAGE_TYPES:
        raise StudyError(
            code="not_supported_yet",
            message="material from page photos is not supported yet",
            fix="send the material as one PDF",
            field_errors=[{"field": "path", "problem": f"{suffix} is a photo, not a PDF"}],
        )
    if suffix != ".pdf":
        raise values.invalid("path", f"{Path(path).name} is not a PDF", "send one PDF file")

    files = intake.read(path, field="path")  # checks the file and hashes it; writes nothing

    with write_unit(engine) as conn:
        course_id = find_course(conn, user_id, code, semester_name)
        found = conn.execute(
            select(material).where(
                material.c.course_id == course_id,
                material.c.content_sha256 == files.sha256,
            )
        ).one_or_none()
        if found is not None:
            return {
                "material_id": found.id,
                "course_id": course_id,
                "filename": found.filename,
                "kind": found.kind,
                "has_text_layer": bool(found.has_text_layer),
                "existing": True,
            }

        intake.keep(files)  # after the duplicate check, before the row (D-44)
        material_id = new_id()
        filename = Path(path).name
        conn.execute(
            insert(material).values(
                id=material_id,
                user_id=user_id,
                course_id=course_id,
                filename=filename,
                file_ref=files.file_ref,
                content_sha256=files.sha256,
                media_type="application/pdf",
                has_text_layer=int(files.has_text_layer),  # 0/1 - Postgres refuses a boolean
                kind=kind,
                added_at=now(),
            )
        )

    return {
        "material_id": material_id,
        "course_id": course_id,
        "filename": filename,
        "kind": kind,
        "has_text_layer": files.has_text_layer,
        "existing": False,
    }
