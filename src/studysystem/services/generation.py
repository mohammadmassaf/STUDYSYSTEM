"""The generation contract, outbound (D-11, D-18, D-45 - D-49): the server hands the host a task -
`{task_id, content, schema, rules}`, followed by a picture of each page that has an image on it -
and never calls a model itself. `study_submit_generation` (1.10) checks what comes back against
the same `schema`.

1.9 builds one kind, `transcription`, for a PDF with a text layer: each page leaves as its text,
plus a picture when a figure sits on it (D-49). A scan or a set of photos waits for 2.6.

One row: the task, written alone (D-45). Starting a paper's transcription again hands back the
same open task and adds the resend to its `bytes_sent` (D-46).

Inbound (1.10, D-50 - D-55): `submit_generation` checks each item against `TRANSCRIPTION_SCHEMA`
alone, keeps the valid ones as `practice_item` rows and returns the rest as item errors - one
write unit per submission, at most three per task.
"""

import base64
import json
import logging
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match
from sqlalchemy import Connection, Engine, func, insert, select, update

from studysystem.db.tables import (
    assessment_slot,
    evidence,
    generation_task,
    past_exam,
    practice_item,
    submission,
    topic,
)
from studysystem.errors import StudyError
from studysystem.services import intake, topics, values
from studysystem.services.ids import new_id, now
from studysystem.services.units import write_unit

# One JSON line per call, after its write commits - enough to answer "what went out?" without a
# debugger (build plan: logs, not tables).
log = logging.getLogger(__name__)

KINDS = ("note", "practice", "memory", "transcription", "timetable")
BUILT = ("transcription",)  # the rest arrive with their tasks (D-47)

# What 1.10 validates each submission against. The server fills in what the host must not get
# wrong: origin = past-exam, past_exam_id, source_marker = from-material, answer_provenance.
TRANSCRIPTION_SCHEMA = {
    "type": "object",
    "required": ["items"],
    "additionalProperties": False,
    "properties": {
        "items": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["position", "question", "topic"],
                "additionalProperties": False,
                "properties": {
                    "position": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "the question's place on the paper: 1, 2, 3... in order",
                    },
                    "question": {
                        "type": "string",
                        "pattern": r"\S",
                        "description": "the question exactly as printed, with its examples",
                    },
                    "answer_key": {
                        "type": ["string", "null"],
                        "pattern": r"\S",
                        "description": "only an answer printed on the paper; otherwise null",
                    },
                    "marks": {
                        "type": ["number", "null"],
                        "minimum": 0,
                        "description": "the marks printed for this question; null if none",
                    },
                    "topic": {
                        "oneOf": [
                            {
                                "type": "object",
                                "required": ["id"],
                                "additionalProperties": False,
                                "properties": {"id": {"type": "string"}},
                            },
                            {
                                "type": "object",
                                "required": ["proposed_name"],
                                "additionalProperties": False,
                                "properties": {
                                    "proposed_name": {"type": "string", "pattern": r"\S"}
                                },
                            },
                        ],
                        "description": "an id from rules.topics, or a new topic's name",
                    },
                },
            },
        }
    },
}

TRANSCRIPTION_STEPS = [
    "Transcribe every question on this past exam paper into items, in paper order.",
    "Copy each question as printed. Never invent, merge or drop a question - these are the real "
    "exam evidence everything else is ranked on.",
    "One item per numbered question. A problem's numbered sub-questions are separate items; "
    "put the problem's shared instructions into each of its items.",
    "position counts items from 1 in paper order.",
    "marks: only what the paper prints for that item; null if it prints only the problem's total.",
    "answer_key: only an answer printed on the paper; otherwise null.",
    "topic: the id of a topic in rules.topics when one fits; otherwise {proposed_name} with a "
    "short topic name, reusing the same name for questions on the same topic.",
    "Submit with study_submit_generation(task_id, payload), payload matching schema.",
]

# Inbound (D-54): the envelope is checked once, then each item alone against its own sub-schema,
# so one bad item fails alone. Both come from TRANSCRIPTION_SCHEMA - the schema the host read.
ITEM_SCHEMA = TRANSCRIPTION_SCHEMA["properties"]["items"]["items"]
ENVELOPE_SCHEMA = {
    **TRANSCRIPTION_SCHEMA,
    "properties": {"items": {"type": "array", "minItems": 1}},
}
_ITEM_CHECK = Draft202012Validator(ITEM_SCHEMA)
_ENVELOPE_CHECK = Draft202012Validator(ENVELOPE_SCHEMA)
MAX_SUBMISSIONS = 3  # D-11


@dataclass(frozen=True)
class Picture:
    """One page picture as it travels: an MCP image block's two fields."""

    mime_type: str
    data: str  # base64 - bytes written as text, which is what the block carries


@dataclass(frozen=True)
class Task:
    """What `start_generation` hands the tool: the JSON payload, then the pictures its
    `content` points at by number (`"image": 1` is `pictures[0]`)."""

    payload: dict
    pictures: list[Picture]


def measure(payload: dict, pictures: list[Picture]) -> int:
    """`bytes_sent` for one send: the UTF-8 length of the JSON the host reads - the same
    `json.dumps` the tool result's text block uses (tools/results.py) - plus the base64 text of
    every picture block, so the count is exactly what went out (D-49)."""
    return len(json.dumps(payload).encode("utf-8")) + sum(len(p.data) for p in pictures)


def start_generation(
    engine: Engine, user_id: str, kind: str, past_exam_id: str | None = None
) -> Task:
    """Hand the host a generation task: the payload {"task_id", "content", "schema", "rules"}
    and the page pictures that follow it."""

    kind = values.choice("kind", kind, KINDS)
    if kind not in BUILT:
        raise StudyError(
            code="not_supported_yet",
            message=f"{kind} generation is not built yet",
            fix=f"only these kinds are available now: {', '.join(BUILT)}",
            field_errors=[{"field": "kind", "problem": "not available yet"}],
        )
    if past_exam_id is None:
        raise values.invalid(
            "past_exam_id", "is missing", "send the past_exam_id that study_add_past_exam returned"
        )

    with write_unit(engine) as conn:
        paper = conn.execute(
            select(past_exam).where(
                past_exam.c.id == past_exam_id,
                past_exam.c.owner_id == user_id,
            )
        ).one_or_none()
        if paper is None:
            raise StudyError(
                code="not_found",
                message=f"no past exam {past_exam_id}",
                fix="use a past_exam_id that study_add_past_exam returned",
                field_errors=[{"field": "past_exam_id", "problem": "no such paper"}],
            )
        if paper.transcript_task_id is not None:
            raise StudyError(
                code="already_transcribed",
                message="this paper is already transcribed",
                fix="nothing to do - its questions are already in the pool",
                field_errors=[{"field": "past_exam_id", "problem": "already transcribed"}],
            )
        if paper.has_text_layer == 0:
            raise StudyError(
                code="not_supported_yet",
                message="this paper is a scan with no text layer",
                fix="scanned papers are read from page images, which are not available yet",
                field_errors=[{"field": "past_exam_id", "problem": "no text layer"}],
            )
        open_task = conn.execute(
            select(generation_task).where(
                generation_task.c.user_id == user_id,
                generation_task.c.kind == "transcription",
                generation_task.c.scope_type == "past-exam",
                generation_task.c.scope_id == paper.id,
                generation_task.c.status == "open",
            )
        ).one_or_none()
        task_id = open_task.id if open_task else new_id()
        task = _transcription_task(conn, paper, task_id)
        size = measure(task.payload, task.pictures)
        if open_task is not None:
            conn.execute(
                update(generation_task)
                .where(generation_task.c.id == open_task.id)
                .values(bytes_sent=generation_task.c.bytes_sent + size)
            )
        else:
            conn.execute(
                insert(generation_task).values(
                    id=task_id,
                    user_id=user_id,
                    kind="transcription",
                    scope_type="past-exam",
                    scope_id=paper.id,
                    route="host",
                    bytes_sent=size,
                    created_at=now(),
                )
            )

    log.info(
        json.dumps(
            {
                "event": "start_generation",
                "task_id": task_id,
                "kind": kind,
                "past_exam_id": paper.id,
                "resend": open_task is not None,
                "pages": len(task.payload["content"]),
                "pictures": len(task.pictures),
                "topics": len(task.payload["rules"]["topics"]),
                "bytes": size,
            }
        )
    )
    return task


def _transcription_task(conn: Connection, paper, task_id: str) -> Task:
    """The task for one paper: its pages as text and pictures, the schema, and the rules with the
    topics of the paper's course that an item may be tagged to."""
    pages = intake.read_pages(paper.file_ref)
    content = []
    pictures = []
    count = 0
    for index, p in enumerate(pages, start=1):
        page = {}
        if p.image is not None:
            assert p.mime_type is not None  # intake sets it with every picture
            picture = Picture(mime_type=p.mime_type, data=base64.b64encode(p.image).decode())
            pictures.append(picture)
            count += 1
            page["image"] = count
        else:
            page["image"] = None
        page["page"] = index
        page["text"] = p.text
        content.append(page)

    course_id = conn.execute(
        select(assessment_slot.c.course_id).where(assessment_slot.c.id == paper.slot_id)
    ).scalar_one()

    rows = conn.execute(
        select(topic.c.id, topic.c.name, topic.c.status)
        .where(topic.c.course_id == course_id, topic.c.status.in_(topics.TAGGABLE))
        .order_by(topic.c.id)
    )
    live = []
    for row in rows:
        live.append(dict(row._mapping))
    payload = {
        "task_id": task_id,
        "content": content,
        "schema": TRANSCRIPTION_SCHEMA,
        "rules": {"steps": TRANSCRIPTION_STEPS, "topics": live},
    }
    return Task(payload=payload, pictures=pictures)


# --- inbound (1.10) -------------------------------------------------------------


@dataclass
class Judged:
    """One submission's items, sorted: `accepted` are the items to write, `errors` the item
    errors to return, `skipped` the positions already on the task (D-52)."""

    accepted: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)


def submit_generation(engine: Engine, user_id: str, task_id: str, payload: Any) -> dict:
    """Take one submission for an open task: keep the valid items, return the rest as item
    errors, and close the task when nothing is wrong or missing (D-11, D-50 - D-55).

    Returns the reply the host reads:
    {"task_id", "submission", "status", "accepted", "skipped", "missing", "item_errors",
     "submissions_left"}
    """

    with write_unit(engine) as conn:
        task = _open_task(conn, user_id, task_id)
        course_id = conn.execute(
            select(assessment_slot.c.course_id)
            .join_from(past_exam, assessment_slot)
            .where(past_exam.c.id == task.scope_id)
        ).scalar_one()
        accepted_before = set(
            conn.execute(select(practice_item.c.position).where(practice_item.c.task_id == task.id))
            .scalars()
            .all()
        )
        env_error = _envelope_error(payload)
        if env_error is not None:
            judged = Judged(errors=[env_error])
        else:
            judged = _judge(conn, user_id, course_id, payload["items"], accepted_before)

        landed = set()
        for item in judged.accepted:
            topic_id = topics.topic_for(conn, user_id, course_id, item["topic"])
            _write_item(conn, task, task.scope_id, item, topic_id)
            landed.add(topic_id)
        # an item on an already-active topic may bring a new slot, which only submit sees (D-60)
        active = conn.execute(
            select(topic.c.id).where(topic.c.id.in_(landed), topic.c.status == "active")
        ).scalars()
        for topic_id in active.all():
            for assessment_id in topics.exam_assessments(conn, topic_id):
                topics.edge_if_missing(conn, user_id, topic_id, assessment_id)
        ordinal = (
            conn.execute(
                select(func.count()).select_from(submission).where(submission.c.task_id == task.id)
            ).scalar_one()
            + 1
        )
        positions = {item["position"] for item in judged.accepted}
        on_task = positions | accepted_before
        max_pos = max(on_task, default=0)
        missing = sorted(set(range(1, max_pos + 1)) - on_task)
        if not missing and not judged.errors:
            status = "closed"
        elif ordinal == MAX_SUBMISSIONS:
            status = "partial"
        else:
            status = "open"
        if status != "open":
            conn.execute(
                update(generation_task)
                .where(generation_task.c.id == task.id)
                .values(status=status, closed_at=now())
            )
        if status == "closed" or (status == "partial" and on_task):
            conn.execute(
                update(past_exam)
                .where(past_exam.c.id == task.scope_id)
                .values(transcript_task_id=task.id)
            )

        reply = {
            "task_id": task.id,
            "submission": ordinal,
            "status": status,
            "accepted": sorted(positions),
            "skipped": sorted(judged.skipped),
            "missing": missing,
            "item_errors": judged.errors,
            "submissions_left": MAX_SUBMISSIONS - ordinal if status == "open" else 0,
        }
        # an envelope error names no item, so it rejects none (D-52)
        rejected = len({e["item_ordinal"] for e in judged.errors} - {None})
        received = len(json.dumps(payload).encode("utf-8"))
        sent = len(json.dumps(reply).encode("utf-8"))
        conn.execute(
            insert(submission).values(
                id=new_id(),
                user_id=user_id,
                task_id=task.id,
                ordinal=ordinal,
                payload=json.dumps(payload),
                validation_result=json.dumps(
                    {
                        "ok": not judged.errors,
                        "item_errors": judged.errors,
                        "skipped": reply["skipped"],
                    }
                ),
                accepted_count=len(judged.accepted),
                rejected_count=rejected,
                bytes_sent=sent,
                bytes_returned=received,
                created_at=now(),
            )
        )
        conn.execute(
            update(generation_task)
            .where(generation_task.c.id == task.id)
            .values(
                bytes_sent=generation_task.c.bytes_sent + sent,
                bytes_returned=generation_task.c.bytes_returned + received,
            )
        )

    log.info(
        json.dumps(
            {
                "event": "submit_generation",
                "task_id": task.id,
                "submission": ordinal,
                "status": status,
                "accepted": len(judged.accepted),
                "rejected": rejected,
                "skipped": len(judged.skipped),
                "bytes": received,
            }
        )
    )
    return reply


def _open_task(conn: Connection, user_id: str, task_id: str):
    """The task a submission is for: this user's, and still open."""
    task = conn.execute(
        select(generation_task).where(
            generation_task.c.id == task_id, generation_task.c.user_id == user_id
        )
    ).one_or_none()
    if task is None:
        raise StudyError(
            code="not_found",
            message=f"no generation task {task_id}",
            fix="use the task_id that study_start_generation returned",
            field_errors=[{"field": "task_id", "problem": "no such task"}],
        )
    if task.status != "open":
        raise StudyError(
            code="task_closed",
            message=f"this task is {task.status} and takes no more submissions",
            fix="nothing to resend - its accepted items are in the pool",
            field_errors=[{"field": "task_id", "problem": f"task is {task.status}"}],
        )
    return task


def _item_error(ordinal: int | None, code: str, field: str, message: str) -> dict:
    """One entry of `item_errors`: `ordinal` is the item's place in this submission, None for
    the envelope (D-52)."""
    return {"item_ordinal": ordinal, "code": code, "field": field, "message": message}


def _envelope_error(payload: Any) -> dict | None:
    """The payload's one envelope error - it is not {"items": [a non-empty list]} - or None."""
    err = best_match(_ENVELOPE_CHECK.iter_errors(payload))
    if err is None:
        return None
    where = "items" if err.path or err.validator == "required" else "payload"
    return _item_error(None, str(err.validator), where, err.message)


TOPIC_SHAPE = 'topic must be {"id": <an id from rules.topics>} or {"proposed_name": <a name>}'


def _schema_errors(ordinal: int, item: Any) -> list[dict]:
    """Every way one item breaks ITEM_SCHEMA, as item errors: code = the failing keyword,
    field = the item field it is about (D-54)."""
    errors = []
    for err in _ITEM_CHECK.iter_errors(item):
        if err.path:  # a field's own value is wrong: marks -9, topic of the wrong shape
            name = str(err.path[0])
            if name == "topic":
                message = TOPIC_SHAPE
            elif err.validator == "pattern":  # the only pattern is \S: blank text
                message = f"{name} must contain at least one non-space character"
            else:
                message = err.message
            errors.append(_item_error(ordinal, str(err.validator), name, message))
        elif err.validator == "required":
            missing = [n for n in err.validator_value if n not in err.instance]
            errors += [
                _item_error(ordinal, "required", n, f"{n!r} is a required property")
                for n in missing
                if not any(e["field"] == n for e in errors)  # one error per missing field
            ]
        elif err.validator == "additionalProperties":
            extra = [k for k in err.instance if k not in ITEM_SCHEMA["properties"]]
            errors += [
                _item_error(ordinal, "additionalProperties", k, f"{k!r} is not an item field")
                for k in extra
            ]
        else:  # the item is not an object at all
            errors.append(_item_error(ordinal, str(err.validator), "item", err.message))
    return errors


def _judge(
    conn: Connection, user_id: str, course_id: str, items: list, accepted_before: set[int]
) -> Judged:
    """Sort one submission's items into accepted, errors and skipped."""
    survivors = []
    accepted = []
    skipped = []
    positions = []
    errors = []
    for place, item in enumerate(items, start=1):
        unvalid = _schema_errors(place, item)
        if unvalid:
            errors.extend(unvalid)
        else:
            survivors.append((item, place))
            positions.append(item["position"])
    count = Counter(positions)
    for item, place in survivors:
        if count[item["position"]] > 1:
            errors.append(
                _item_error(
                    place,
                    "duplicate_position",
                    "position",
                    f"position {item['position']} is on more than one item - give each "
                    "question its own position",
                )
            )
        elif item["position"] in accepted_before:
            skipped.append(item["position"])
        elif "id" in item["topic"] and not topics.is_live_topic(
            conn, user_id, course_id, item["topic"]["id"]
        ):
            errors.append(
                _item_error(
                    place,
                    "unknown_topic",
                    "topic",
                    f"{item['topic']['id']} is not a topic of this course - use an id from "
                    "rules.topics, or {proposed_name} for a new one",
                )
            )
        else:
            accepted.append(item)

    errors.sort(
        key=lambda e: e["item_ordinal"]
    )  # loop A's errors came first; put them in item order
    return Judged(accepted, errors, skipped)


def _write_item(conn: Connection, task, paper_id: str, item: dict, topic_id: str) -> None:
    """One accepted item: its practice_item row and its E1 evidence row (D-50)."""
    item_id = new_id()
    conn.execute(
        insert(practice_item).values(
            id=item_id,
            user_id=task.user_id,
            topic_id=topic_id,
            task_id=task.id,
            item_ordinal=item["position"],
            origin="past-exam",
            past_exam_id=paper_id,
            position=item["position"],
            question=item["question"],
            answer_key=item.get("answer_key"),
            answer_provenance="none" if item.get("answer_key") is None else "verified-source",
            marks=item.get("marks"),
            state="live",
            source_marker="from-material",
            novelty_score=None,
            nearest_item_id=None,
            created_at=now(),
        )
    )
    conn.execute(
        insert(evidence).values(
            id=new_id(),
            user_id=task.user_id,
            practice_item_id=item_id,
            study_tier="E1",
            locator=None,
            created_at=now(),
        )
    )
