"""Past exams (D-14, D-42, D-43, D-44): a paper registered under an existing slot - the slot is the
paper pool the exam profile derives from, so a paper in the wrong slot is evidence lost.

One row, so no Write-units entry. The server's copy of the file is written inside the same unit,
just before the row: a refusal leaves no copy behind, and a copy whose row fails is reused on the
retry (it is named by its hash).
"""

from sqlalchemy import Engine, insert, select

from studysystem.db.tables import past_exam
from studysystem.errors import StudyError
from studysystem.services import intake, values
from studysystem.services.ids import new_id, now
from studysystem.services.lookups import find_course, find_slot
from studysystem.services.units import write_unit

SESSION_TYPES = ("first", "second")


def add_past_exam(
    engine: Engine,
    user_id: str,
    code: str,
    assessment_name: str,
    session_type: str,
    session_date: str,
    paths: list[str] | str,
    instructor: str | None = None,
    session_delayed: bool | str = False,
    semester_name: str | None = None,
) -> dict:
    """Register one paper. Returns {"past_exam_id", "slot_id", "file_ref", "has_text_layer",
    "pages"} - `pages` is the stored page names, so a swapped photo can be spotted."""

    session_type = values.choice("session_type", session_type, SESSION_TYPES)
    date = values.day("session_date", session_date)
    delayed = values.flag("session_delayed", session_delayed)
    if instructor is not None:  # left out = unknown; given = must be real text
        instructor = values.text("instructor", instructor)

    files = intake.read(paths)

    with write_unit(engine) as conn:
        course_id = find_course(conn, user_id, code, semester_name)
        slot_id = find_slot(conn, user_id, course_id, assessment_name)
        # Same bytes under the same slot - the unique index's rule, checked first (D-36).
        exists = conn.execute(
            select(past_exam.c.id).where(
                past_exam.c.slot_id == slot_id, past_exam.c.content_sha256 == files.sha256
            )
        ).scalar_one_or_none()
        if exists is not None:
            raise StudyError(
                code="past_exam_exists",
                message=f"this paper is already registered under {assessment_name}",
                fix="nothing to do - this paper is already in the pool",
                field_errors=[{"field": "paths", "problem": "same file already registered"}],
            )
        pages = intake.keep(files)  # after every refusal, before the row (D-44)
        past_exam_id = new_id()
        conn.execute(
            insert(past_exam).values(
                id=past_exam_id,
                owner_id=user_id,
                slot_id=slot_id,
                session_type=session_type,
                session_date=date,
                session_delayed=int(delayed),  # 0/1 columns - Postgres refuses a boolean
                instructor=instructor,
                instructor_tier="declared" if instructor is not None else "unknown",
                has_text_layer=int(files.has_text_layer),
                file_ref=files.file_ref,
                content_sha256=files.sha256,
                created_at=now(),
            )
        )

    return {
        "past_exam_id": past_exam_id,
        "slot_id": slot_id,
        "file_ref": files.file_ref,
        "has_text_layer": files.has_text_layer,
        "pages": pages,
    }
