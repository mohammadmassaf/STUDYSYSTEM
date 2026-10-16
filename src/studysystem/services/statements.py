"""Instructor statements (2.4; D-14, D-172, D-173, D-180): what the instructor said about the
coming exam - a topic's share, or a format fact - entered as an E2 observation with no paper.
`derive_profile` reads the ones still counting and lets them outrank what the papers' age implies
(D-177 - D-179).

`study_add_instructor_statement` is one write unit: the observation and its evidence row
(physical-schema.md, *Write units*).
"""

import json
from typing import Any

from jsonschema import Draft202012Validator
from sqlalchemy import Engine, insert, select

from studysystem.db.tables import CLAIM_KEYS, evidence, observation, topic
from studysystem.errors import StudyError
from studysystem.services.claims import CLASS_OF
from studysystem.services.generation import CLAIM_VALUE_SCHEMAS
from studysystem.services.ids import new_id, now
from studysystem.services.lookups import find_course, find_slot
from studysystem.services.profiles import FULL, counting_statements, paper_set, pins_of
from studysystem.services.units import write_unit

# The keys a statement may carry (D-172): a topic's share, or a format fact. Instructor-driven
# keys are not offered - they have no age rule to outrank.
STATEMENT_KEYS = ["topic-marks", *CLAIM_KEYS["format"]]
VALUE_SCHEMAS = {
    "topic-marks": {
        "type": "object",
        "required": ["marks", "of_total"],
        "additionalProperties": False,
        "properties": {
            "marks": {"type": "number", "minimum": 0},
            "of_total": {"type": "number", "exclusiveMinimum": 0},
        },
    },
    **{key: CLAIM_VALUE_SCHEMAS[key] for key in CLAIM_KEYS["format"]},
}
LIVE_TOPICS = ("active", "unexamined")  # a confirmed topic (D-173)


def _refuse(code: str, field: str, problem: str, fix: str) -> StudyError:
    return StudyError(
        code=code,
        message=f"{field}: {problem}",
        fix=fix,
        field_errors=[{"field": field, "problem": problem}],
    )


def _check_value(claim_key: str, claim_value: Any) -> None:
    """The value's shape for its key - the observation task's shapes for format keys - and,
    for `topic-marks`, marks no more than the total."""
    errors = sorted(
        Draft202012Validator(VALUE_SCHEMAS[claim_key]).iter_errors(claim_value), key=str
    )
    if errors:
        raise _refuse(
            "invalid_value",
            "claim_value",
            errors[0].message,
            f"send claim_value shaped {json.dumps(VALUE_SCHEMAS[claim_key]['properties'])}",
        )
    if claim_key == "topic-marks" and claim_value["marks"] > claim_value["of_total"]:
        raise _refuse(
            "invalid_value",
            "claim_value",
            "marks is more than of_total",
            'send the topic\'s marks out of the exam\'s total, e.g. {"marks": 30, "of_total": 100}',
        )


def add_instructor_statement(
    engine: Engine,
    user_id: str,
    code: str,
    assessment_name: str,
    claim_key: str,
    claim_value: Any,
    topic_id: str | None = None,
    citation: str | None = None,
    semester_name: str | None = None,
) -> dict:
    """Store one thing the instructor said about this assessment's coming exam (D-173), as one
    write unit: an `observation` (E2, `past_exam_id` NULL, class fixed by key) and its `evidence`
    row. The next derive uses it until a paper dated after it joins the profile's set (D-179).

    Refuses `invalid_value` for a key outside STATEMENT_KEYS, a value of the wrong shape, a
    missing topic on `topic-marks` or a topic on a format key; `not_found` for a topic that is
    not a confirmed topic of this course; `pins_over_100` when, with this one, the slot's
    `topic-marks` statements still counting - the newest per topic (D-177) - pin more than 100
    (D-180).

    Returns {"observation_id", "claim_key", "claim_value", "topic": {"id", "name"} or None,
    "citation", "stated"} - `stated` the statement in words, to repeat to the student.
    """
    if claim_key not in STATEMENT_KEYS:
        raise _refuse(
            "invalid_value",
            "claim_key",
            f"{claim_key!r} is not a key an instructor statement carries",
            f"use one of {', '.join(STATEMENT_KEYS)} - traps and phrasing come from past papers",
        )
    _check_value(claim_key, claim_value)
    if claim_key == "topic-marks" and topic_id is None:
        raise _refuse(
            "invalid_value",
            "topic_id",
            "a topic's share needs the topic",
            "send the topic's id from study_get_topics",
        )
    if claim_key != "topic-marks" and topic_id is not None:
        raise _refuse(
            "invalid_value",
            "topic_id",
            f"{claim_key} is about the whole exam, not a topic",
            "leave topic_id out",
        )

    with write_unit(engine) as conn:
        course_id = find_course(conn, user_id, code, semester_name)
        slot_id = find_slot(conn, user_id, course_id, assessment_name)

        topic_name = None
        if topic_id is not None:
            topic_name = conn.execute(
                select(topic.c.name).where(
                    topic.c.id == topic_id,
                    topic.c.user_id == user_id,
                    topic.c.course_id == course_id,
                    topic.c.status.in_(LIVE_TOPICS),
                )
            ).scalar_one_or_none()
            if topic_name is None:
                raise _refuse(
                    "not_found",
                    "topic_id",
                    "no confirmed topic of this course has this id",
                    "use an id from study_get_topics for this course",
                )

            # D-180: with this one, the shares still counting must leave room for the rest.
            in_set = paper_set(conn, user_id, slot_id)
            newest_paper = in_set[0][1] if in_set else ""
            new = {"claim_key": claim_key, "claim_value": claim_value, "topic_id": topic_id}
            pins = pins_of(counting_statements(conn, user_id, slot_id, newest_paper) + [new])
            if sum(pins.values()) > FULL:
                raise _refuse(
                    "pins_over_100",
                    "claim_value",
                    f"with this one, the instructor's stated shares come to "
                    f"{sum(pins.values()):g}% of the exam",
                    "check what the instructor said; a newer statement on the same topic "
                    "replaces the older one",
                )

        observation_id = new_id()
        conn.execute(
            insert(observation).values(
                id=observation_id,
                owner_id=user_id,
                slot_id=slot_id,
                past_exam_id=None,
                claim_key=claim_key,
                claim_class=CLASS_OF[claim_key],
                topic_id=topic_id,
                claim_value=json.dumps(claim_value),
                study_tier="E2",
                citation=citation,
                created_at=now(),
            )
        )
        conn.execute(
            insert(evidence).values(
                id=new_id(),
                user_id=user_id,
                observation_id=observation_id,
                study_tier="E2",
                locator=citation,
                created_at=now(),
            )
        )

    return {
        "observation_id": observation_id,
        "claim_key": claim_key,
        "claim_value": claim_value,
        "topic": {"id": topic_id, "name": topic_name} if topic_id else None,
        "citation": citation,
        "stated": f"{code} {assessment_name}, from the instructor: "
        f"{_in_words(claim_key, claim_value, topic_name)}. Derive the profile again to use it.",
    }


def _in_words(claim_key: str, value: dict, topic_name: str | None) -> str:
    """One statement as the student would say it: "PHP + MySQL is 40 of 100 marks"."""
    if claim_key == "topic-marks":
        if value["marks"] == 0:
            return f"{topic_name} is not on the exam"
        return f"{topic_name} is {value['marks']:g} of {value['of_total']:g} marks"
    if claim_key == "duration-minutes":
        return f"the exam lasts {value['minutes']} minutes"
    if claim_key == "total-marks":
        return f"the exam is out of {value['marks']:g} marks"
    if claim_key == "question-count":
        return f"the exam has {value['count']} questions"
    return "the exam has MCQ" if value["value"] else "the exam has no MCQ"  # has-mcq
