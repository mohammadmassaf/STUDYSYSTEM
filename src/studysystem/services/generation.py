"""The generation contract, outbound (D-11, D-18, D-45 - D-49): the server hands the host a task -
`{task_id, content, schema, rules}`, followed by a picture of each page that has an image on it -
and never calls a model itself. `study_submit_generation` (1.10) checks what comes back against
the same `schema`.

1.9 builds one kind, `transcription`, for a PDF with a text layer: each page leaves as its text,
plus a picture when a figure sits on it (D-49). A scan or a set of photos waits for 2.6.

One row: the task, written alone (D-45). Starting a paper's transcription again hands back the
same open task and adds the resend to its `bytes_sent` (D-46).
"""

import base64
import json
import logging
from dataclasses import dataclass

from sqlalchemy import Connection, Engine, insert, select, update

from studysystem.db.tables import assessment_slot, generation_task, past_exam, topic
from studysystem.errors import StudyError
from studysystem.services import intake, values
from studysystem.services.ids import new_id, now
from studysystem.services.units import write_unit

# One JSON line per call, after its write commits - enough to answer "what went out?" without a
# debugger (build plan: logs, not tables).
log = logging.getLogger(__name__)

KINDS = ("note", "practice", "memory", "transcription", "timetable")
BUILT = ("transcription",)  # the rest arrive with their tasks (D-47)

# Topics a transcribed item may be tagged to: live ones, `proposed` included (1.10 accepts a
# proposal). `superseded` and `declined` topics are retired and never offered.
TAGGABLE = ("proposed", "active", "unexamined")

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
                        "minLength": 1,
                        "description": "the question exactly as printed, with its examples",
                    },
                    "answer_key": {
                        "type": ["string", "null"],
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
                                "properties": {"proposed_name": {"type": "string", "minLength": 1}},
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
        .where(topic.c.course_id == course_id, topic.c.status.in_(TAGGABLE))
        .order_by(topic.c.id)
    )
    topics = []
    for row in rows:
        topics.append(dict(row._mapping))
    payload = {
        "task_id": task_id,
        "content": content,
        "schema": TRANSCRIPTION_SCHEMA,
        "rules": {"steps": TRANSCRIPTION_STEPS, "topics": topics},
    }
    return Task(payload=payload, pictures=pictures)
