"""Assessments (D-35): a slot and its sitting, added together, then their known-before-the-exam
inputs set one at a time.

The slot is the recurring component ("Final exam") and owns the past-paper pool; the
`assessment` row is this term's sitting - weight, date, time, room (ticket 21).
"""

from sqlalchemy import Connection, Engine, func, insert, select, update  # noqa: F401

from studysystem.db.tables import assessment, assessment_slot  # noqa: F401
from studysystem.errors import StudyError  # noqa: F401
from studysystem.services import values  # noqa: F401
from studysystem.services.ids import new_id, now  # noqa: F401
from studysystem.services.lookups import find_assessment, find_course  # noqa: F401
from studysystem.services.units import write_unit  # noqa: F401

KINDS = ("exam", "project", "lab")

# What study_set_assessment_input accepts. Status and mark are the post-exam flow's, not 1.7's.
ASSESSMENT_FIELDS = ("weight", "date", "time", "room")


def insert_slot_and_assessment(
    conn: Connection,
    user_id: str,
    course_id: str,
    name: str,
    kind: str,
    weight: float | None,
    weight_tier: str,
) -> dict:
    """Helper: one slot and its one sitting, on the caller's unit. Shared by `add_course` (the
    default final) and `add_assessment`, so the session-type rule lives in one place.
    Returns {"slot_id", "assessment_id"}."""
    # TODO(human):
    #   Every value either arrives as an argument or is made here; nothing is looked up.
    #   Inputs are already checked by the caller - this only writes.
    #
    #   1. make the ids and the timestamp you will need
    #        (the assessment row points at the slot's id - when must that id exist?)
    #
    #   2. insert one assessment_slot row:
    #        id          <- a new id
    #        owner_id    <- the user_id argument (the evidence cluster names it owner_id)
    #        course_id   <- argument
    #        name        <- argument            e.g. "Final exam"
    #        kind        <- argument            e.g. "exam"
    #        created_at  <- now
    #
    #   3. insert one assessment row:
    #        id           <- another new id
    #        user_id      <- the user_id argument
    #        slot_id      <- the slot's id from step 2
    #        weight       <- argument            e.g. 100, or None
    #        weight_tier  <- argument            e.g. "inferred" - no column default,
    #                                            so leaving it out fails the insert
    #        session_type <- "first" if the kind is exam, else nothing (NULL)
    #        created_at   <- now
    #      leave out date, date_approx, time, room, status, mark - their defaults
    #      (NULL, 0, NULL, NULL, "upcoming", NULL) are what a new sitting is
    #
    #   4. hand back {"slot_id": ..., "assessment_id": ...}

    slot_id = new_id()
    created_at = now()

    conn.execute(
        insert(assessment_slot).values(
            id=slot_id,
            owner_id=user_id,
            course_id=course_id,
            name=name,
            kind=kind,
            created_at=created_at,
        )
    )
    assessment_id = new_id()

    conn.execute(
        insert(assessment).values(
            id=assessment_id,
            user_id=user_id,
            slot_id=slot_id,
            weight=weight,
            weight_tier=weight_tier,
            session_type="first" if kind == "exam" else None,
            created_at=created_at,
        )
    )

    return {"slot_id": slot_id, "assessment_id": assessment_id}


def add_assessment(
    engine: Engine,
    user_id: str,
    code: str,
    name: str,
    kind: str,
    weight: float | str | None = None,
    semester_name: str | None = None,
) -> dict:
    """A new slot + sitting on an existing course. A weight given is declared; none is unknown.
    Returns {"course_id", "slot_id", "assessment_id"}."""
    # TODO(human):
    #   before the lock: check name, kind and (if given) weight
    #   in one unit:
    #     find the course
    #     a slot of that course already has this name (any case) -> slot_exists,
    #       the fix pointing at study_set_assessment_input
    #     insert through the helper, with the tier that matches whether a weight came in
    raise NotImplementedError


def set_assessment_input(
    engine: Engine,
    user_id: str,
    code: str,
    assessment_name: str,
    field: str,
    value: float | str | None,
    semester_name: str | None = None,
) -> dict:
    """Set one input on a sitting (D-37: setting declares, no way back to unknown).
    Returns {"assessment_id", "field", "value"} plus the tier or approx flag it changed."""
    # TODO(human):
    #   before the lock:
    #     field not one of ASSESSMENT_FIELDS -> unknown_field; the fix lists them, and for
    #       credits / instructor / target_grade points at study_set_course_input
    #     no value -> invalid_value (D-37)
    #     check the value by field: weight 0-100 | date, exact or month (D-39) | time | room text
    #   in one unit: find the course, then the assessment, then update only the columns this
    #     field owns - weight moves its tier to declared, date moves the approx flag with it
    raise NotImplementedError
