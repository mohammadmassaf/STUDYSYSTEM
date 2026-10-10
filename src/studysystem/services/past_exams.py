"""Past exams (D-14, D-42, D-43, D-44, D-137): a paper registered under an existing slot - the
slot is the paper pool the exam profile derives from, so a paper in the wrong slot is evidence
lost.

One row, so no Write-units entry. The server's copy of the file is written inside the same unit,
just before the row: a refusal leaves no copy behind, and a copy whose row fails is reused on the
retry (it is named by its hash).
"""

import re

from sqlalchemy import Engine, insert, select

from studysystem.db.tables import past_exam
from studysystem.errors import StudyError
from studysystem.services import intake, values
from studysystem.services.ids import new_id, now
from studysystem.services.lookups import find_course, find_slot
from studysystem.services.units import write_unit

SESSION_TYPES = ("first", "second")
PAPER_DATE = (
    "send the day the paper prints as YYYY-MM-DD, or YYYY-MM if it prints only the month - "
    "never a guessed day"
)


def paper_date(value: object) -> tuple[str, bool]:
    """The printed day, or the printed month as its 1st, marked approximate (D-43, D-137).
    Returns (the date to store, whether it is approximate). Refusals carry a fix that names only
    these two forms - the shared checkers' fixes offer `~YYYY-MM-DD`, or a day to invent."""
    if isinstance(value, str) and re.fullmatch(r"[0-9]{4}-[0-9]{2}", value):
        if not 1 <= int(value[5:]) <= 12:
            raise values.invalid(
                "session_date", f"{value}: there is no month {value[5:]}", PAPER_DATE
            )
        return f"{value}-01", True
    try:
        return values.day("session_date", value), False
    except StudyError as err:
        raise values.invalid("session_date", err.field_errors[0]["problem"], PAPER_DATE) from None


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
    "pages", "session_date", "session_date_approx"} - `pages` is the stored page names, so a
    swapped photo can be spotted.

    `session_date` is the day the paper prints (D-43), or `YYYY-MM` when it prints only the
    month: stored as the 1st, marked approximate (D-137, D-39's form)."""

    session_type = values.choice("session_type", session_type, SESSION_TYPES)
    date, approx = paper_date(session_date)
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
                session_date_approx=int(approx),
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
        "session_date": date,
        "session_date_approx": approx,
    }
