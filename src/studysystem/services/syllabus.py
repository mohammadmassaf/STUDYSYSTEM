"""Syllabus topics (3.9; D-218, D-222, D-224): a course with no past papers gets its topics from
its syllabus's learning outcomes, so the plan can rank it from the start of term.

`study_add_syllabus_topics` is one write unit (physical-schema.md, *Write units*): one `active`
topic per outcome and an `inferred` edge from each to the exam that examines the whole syllabus.
No edge goes to any other exam: a Partial reaches an outcome only through a fed chapter (D-222).
"""

from sqlalchemy import Engine, func, insert, select

from studysystem.db.tables import assessment, assessment_slot, topic
from studysystem.errors import StudyError
from studysystem.services.ids import new_id, now
from studysystem.services.lookups import find_course, find_slot
from studysystem.services.topics import TAGGABLE, edge_if_missing
from studysystem.services.units import write_unit


def _outcome_shape_errors(outcomes) -> list[dict]:
    """One field error per malformed part of `outcomes`, checked before the database opens: a
    non-empty list of non-blank names, distinct ignoring case."""
    if not isinstance(outcomes, list) or not outcomes:
        return [{"field": "outcomes", "problem": "must be a non-empty list of outcome names"}]
    errors = []
    seen = {}
    for i, name in enumerate(outcomes):
        if not isinstance(name, str) or not name.strip():
            errors.append({"field": f"outcomes[{i}]", "problem": "must be a non-blank name"})
            continue
        key = name.strip().lower()
        if key in seen:
            errors.append({"field": f"outcomes[{i}]", "problem": f"same as outcomes[{seen[key]}]"})
        seen.setdefault(key, i)
    return errors


def add_syllabus_topics(
    engine: Engine,
    user_id: str,
    code: str,
    assessment_name: str,
    outcomes,
    semester_name: str | None = None,
) -> dict:
    """The syllabus's learning outcomes as the course's topics, as one write unit (D-218).

    `assessment_name` is the exam that examines the whole syllabus (I3351: "Final exam"); it
    must be an exam. Each name in `outcomes` becomes an `active` topic, `proposed_by =
    'syllabus'`, with an `inferred` edge to every sitting of that exam - a resit is a second
    sitting of the same slot. No name may be a live topic's of the course (as a split's, D-201).

    I3351, "Final exam", 9 outcomes -> 9 topics, 9 edges to the Final, none to the Partial.

    Returns {"course_id", "assessment", "topics": [{"topic_id", "name"}], "stored"} - `stored`
    repeats what was written, for the host to read back to the student (D-224).
    """
    shape = _outcome_shape_errors(outcomes)
    if shape:
        raise StudyError(
            code="invalid_value",
            message="outcomes must be a list of distinct, non-blank names",
            fix="send one name per learning outcome, e.g. ['LO1 Explain the role of a system "
            "administrator', ...]",
            field_errors=shape,
        )

    with write_unit(engine) as conn:
        course_id = find_course(conn, user_id, code, semester_name)
        slot_id = find_slot(conn, user_id, course_id, assessment_name)
        slot = conn.execute(
            select(assessment_slot.c.name, assessment_slot.c.kind).where(
                assessment_slot.c.id == slot_id
            )
        ).one()
        if slot.kind != "exam":
            raise StudyError(
                code="invalid_value",
                message=f"{slot.name} is a {slot.kind}, not an exam",
                fix="name the exam that examines the whole syllabus, usually the Final",
                field_errors=[{"field": "assessment", "problem": "not an exam"}],
            )

        # names: none may be a live topic's of this course, compared as the partial unique
        # index compares them
        problems = []
        for i, name in enumerate(outcomes):
            taken = conn.execute(
                select(topic.c.name).where(
                    topic.c.user_id == user_id,
                    topic.c.course_id == course_id,
                    func.lower(topic.c.name) == func.lower(name.strip()),
                    topic.c.status.in_(TAGGABLE),
                )
            ).scalar_one_or_none()
            if taken is not None:
                problems.append(
                    {"field": f"outcomes[{i}]", "problem": f"live topic {taken!r} has this name"}
                )
        if problems:
            raise StudyError(
                code="invalid_value",
                message="some outcomes are already topics of this course",
                fix="call study_get_topics to see the course's topics, and send only new outcomes",
                field_errors=problems,
            )

        sittings = (
            conn.execute(select(assessment.c.id).where(assessment.c.slot_id == slot_id))
            .scalars()
            .all()
        )
        stamp = now()
        made = []
        for name in outcomes:
            topic_id = new_id()
            conn.execute(
                insert(topic).values(
                    id=topic_id,
                    user_id=user_id,
                    course_id=course_id,
                    name=name.strip(),
                    status="active",  # nothing to confirm it against: he read the syllabus
                    proposed_by="syllabus",
                    status_changed_at=stamp,
                    created_at=stamp,
                )
            )
            for assessment_id in sittings:
                edge_if_missing(conn, user_id, topic_id, assessment_id)
            made.append({"topic_id": topic_id, "name": name.strip()})

    return {
        "course_id": course_id,
        "assessment": slot.name,
        "topics": made,
        "stored": f"{code}: {len(made)} syllabus topics, each counted on the {slot.name} "
        f"(an equal share until the instructor states one): " + "; ".join(t["name"] for t in made),
    }
