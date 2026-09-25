"""Finding a course by code and an assessment by name (D-38) - how every course-scoped tool turns
what Mohammad says ("I3302-E", "Midterm") into row ids.

Both take the caller's `conn`, never an engine: a setter looks up and writes inside one unit
(D-33). Both filter on `user_id`, so another student's course is `not_found`, never a match.
"""

from sqlalchemy import Connection, func, select  # noqa: F401 - the queries below use them

from studysystem.db.tables import assessment, assessment_slot, course  # noqa: F401
from studysystem.errors import StudyError  # noqa: F401


def find_course(conn: Connection, user_id: str, code: str, semester_name: str | None) -> str:
    """The id of this user's course with `code`, matched case-insensitively like the unique
    index. `semester_name` narrows it when the code exists in more than one semester."""
    # TODO(human):
    #   this user's courses whose code matches, ignoring case; also the semester when given
    #   none -> not_found, field `code`
    #   more than one -> ambiguous_course; the fix names each semester and says to pass one
    #   exactly one -> its id
    raise NotImplementedError


def find_assessment(conn: Connection, user_id: str, course_id: str, name: str) -> str:
    """The id of the assessment under this course's slot called `name` (case-insensitive).
    One per slot in 1.7 - nothing adds a resit sitting yet."""
    # TODO(human):
    #   the slot of this course whose name matches, ignoring case, and its assessment row
    #   none -> not_found, field `assessment`; the fix lists the slot names this course has,
    #     so "Mid-term" shows the model that "Midterm" exists
    #   found -> the assessment's id
    raise NotImplementedError
