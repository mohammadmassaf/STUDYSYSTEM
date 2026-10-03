"""The generation contract, outbound (D-11, D-18, D-45 - D-49): the server hands the host a task -
`{task_id, content, schema, rules}`, followed by a picture of each page that has an image on it -
and never calls a model itself. `study_submit_generation` (1.10) checks what comes back against
the same `schema`.

Two kinds are built, each for a PDF with a text layer - each page leaves as its text, plus a
picture when a figure sits on it (D-49); a scan or a set of photos waits for 2.6:
- `transcription` (1.9 - 1.10, D-50 - D-55): a past paper becomes `practice_item` rows.
- `note` (1.14, D-70 - D-74): a material becomes one `note`, aimed by the course's profile - its
  weights and the past-exam items behind them - then copied into the vault (`notes.py`, D-72).

Start writes one row, the task (D-45); starting again while it is open hands back the same task
and adds the resend to its `bytes_sent` (D-46). Submit checks the envelope once, then each item
alone against the kind's schema, keeps the valid ones and returns the rest as item errors - one
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
    exam_profile,
    generation_task,
    material,
    note,
    past_exam,
    practice_item,
    submission,
    topic,
    topic_weight,
)
from studysystem.errors import StudyError
from studysystem.services import intake, notes, topics, values
from studysystem.services.ids import new_id, now
from studysystem.services.units import write_unit

# One JSON line per call, after its write commits - enough to answer "what went out?" without a
# debugger (build plan: logs, not tables).
log = logging.getLogger(__name__)

KINDS = ("note", "practice", "memory", "transcription", "timetable")
BUILT = ("transcription", "note")  # the rest arrive with their tasks (D-47)

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

# A note task's answer (1.14, D-70, D-73): one entry per section of the material, the body as
# the host wrote it. An array although 1.14 sends one section, so D-11's split by section
# changes no schema when it lands. No topic field - a note tags no topic (D-70).
NOTE_SCHEMA = {
    "type": "object",
    "required": ["notes"],
    "additionalProperties": False,
    "properties": {
        "notes": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["section", "body"],
                "additionalProperties": False,
                "properties": {
                    "section": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "which section of the material this note covers: 1",
                    },
                    "body": {
                        "type": "string",
                        "pattern": r"\S",
                        "description": "the whole note, as markdown, with no frontmatter",
                    },
                },
            },
        }
    },
}

# Inbound, as for transcription (D-54): the envelope once, then each entry alone.
NOTE_ENTRY_SCHEMA = NOTE_SCHEMA["properties"]["notes"]["items"]
_NOTE_ENTRY_CHECK = Draft202012Validator(NOTE_ENTRY_SCHEMA)
_NOTE_ENVELOPE_CHECK = Draft202012Validator(
    {**NOTE_SCHEMA, "properties": {"notes": {"type": "array", "minItems": 1}}}
)
# The sections a note task expects. One until D-11's split by section lands (D-73): that task
# replaces this with a per-task count.
NOTE_SECTIONS = frozenset({1})

# How to write the note - add-pdf's Pipeline A §3-5, ported (D-74): the host gets no vault, so
# everything it needs to aim the note travels in `rules`. Dropped and changed rows: D-74.
NOTE_STEPS = [
    "Write one exam-ready study note for this material, aimed by the exam evidence in rules - "
    "not by how much space the pages give each part.",
    "Read every page before writing: the last pages often show what the material builds toward.",
    "Write in the language of the pages, and keep their terminology exactly.",
    "Evidence: rules.topics gives each topic's share of past exams (the weights sum to 100); "
    "rules.past_exam_items are the real questions behind those weights. If both are empty, the "
    "course has no exam evidence yet: say once, at the top, that the note is provisional, and "
    "lean on the E2 signals.",
    "While reading, collect E2 signals - repeated across pages, bold, boxed, marked important, "
    "or given more space than its length needs - with the page numbers where each appears.",
    "Exam positioning, the note's first section: which topics in rules.topics this material "
    "teaches and their combined share of the exam; which past-exam items it prepares for; what "
    "those questions reward; and what in the material no evidence supports, as a line "
    "'Not examined - don't invest: ...'.",
    "Draft the To-Memorize table before any prose: | # | Statement / syntax / formula | Tier | "
    "Evidence |. E1 = asked in a past exam, cite the item (e.g. 'Final 2020-02-17 Q3'); E2 = "
    "instructor emphasis, cite the pages; E3 = your judgment, cite 'examinable, no signal yet'. "
    "Tier order is study priority; keep E3 rows few.",
    "Layer 1 - Summary: the whole material compressed for a night-before read, 700-900 words at "
    "most. Compress; never repeat Layer 2 word for word.",
    "Layer 2 - Core notes: definitions, syntax, functions and algorithms, each with a worked "
    "example, as a strong student takes them in class. If you can run code, run every worked "
    "example and show its real output.",
    "Layer 3 - Depth & pitfalls: edge cases and tricky details, only where an exam could ask them.",
    "Markers: 🎯 likely exam target - justify each from rules, citing the item or the weight; "
    "💡 tip or shortcut; ⚠️ common mistake. There is no mistake history yet, so say once that the "
    "⚠️ items are generic.",
    "Order: a title; the legend line '🎯 likely exam target · 💡 tip · ⚠️ common mistake'; Exam "
    "positioning; Layer 1; Layer 2; Layer 3; To-Memorize; Self-check - 3 to 5 recall prompts, "
    "no answers. No frontmatter: the server adds it.",
    "Submit with study_submit_generation(task_id, payload): "
    '{"notes": [{"section": 1, "body": the whole note as markdown}]}.',
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
    engine: Engine,
    user_id: str,
    kind: str,
    past_exam_id: str | None = None,
    material_id: str | None = None,
) -> Task:
    """Hand the host a generation task: the payload {"task_id", "content", "schema", "rules"}
    and the page pictures that follow it. Each kind takes the parameter named for its subject
    (D-47): `past_exam_id` for a transcription, `material_id` for a note."""

    kind = values.choice("kind", kind, KINDS)
    if kind not in BUILT:
        raise StudyError(
            code="not_supported_yet",
            message=f"{kind} generation is not built yet",
            fix=f"only these kinds are available now: {', '.join(BUILT)}",
            field_errors=[{"field": "kind", "problem": "not available yet"}],
        )
    if kind == "note":
        return _start_note(engine, user_id, material_id)
    return _start_transcription(engine, user_id, kind, past_exam_id)


def _start_transcription(engine: Engine, user_id: str, kind: str, past_exam_id: str | None) -> Task:
    """One paper's transcription task, written alone - or the same open task again (D-45, D-46)."""
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
    content, pictures = _pages(paper.file_ref)

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


def _start_note(engine: Engine, user_id: str, material_id: str | None) -> Task:
    """One material's note task, written alone - or the same open task again (D-46, D-71,
    D-73, D-74). A material with a `note` row is refused, whatever its task's status: a task
    can keep a note and still end `partial` or `invalidated` (section 1 accepted, section 2 an
    error). A task that kept no note lets the material start again (D-76)."""
    if material_id is None:
        raise values.invalid(
            "material_id", "is missing", "send the material_id that study_add_material returned"
        )

    with write_unit(engine) as conn:
        found = conn.execute(
            select(material).where(material.c.id == material_id, material.c.user_id == user_id)
        ).one_or_none()
        if found is None:
            raise StudyError(
                code="not_found",
                message=f"no material {material_id}",
                fix="use a material_id that study_add_material returned",
                field_errors=[{"field": "material_id", "problem": "no such material"}],
            )
        if found.has_text_layer == 0:
            raise StudyError(
                code="not_supported_yet",
                message="this material is a scan with no text layer",
                fix="scanned material is read from page images, which are not available yet",
                field_errors=[{"field": "material_id", "problem": "no text layer"}],
            )
        noted = conn.execute(
            select(note.c.id).where(note.c.material_id == found.id).limit(1)
        ).first()
        if noted is not None:
            raise StudyError(
                code="already_noted",
                message=f"{found.filename} already has its note",
                fix="nothing to do - its note is saved in the database",
                field_errors=[{"field": "material_id", "problem": "already has a note"}],
            )
        # At most one open task per material: start reuses it, so a second one is a bug -
        # `one_or_none` raises on it rather than picking one quietly.
        open_task = conn.execute(
            select(generation_task).where(
                generation_task.c.user_id == user_id,
                generation_task.c.kind == "note",
                generation_task.c.scope_type == "material",
                generation_task.c.scope_id == found.id,
                generation_task.c.status == "open",
            )
        ).one_or_none()

        task_id = open_task.id if open_task else new_id()
        profile = _current_profile(conn, user_id, found.course_id)
        # Built before any write: a missing file copy fails here, with nothing written.
        task = _note_task(conn, found, task_id, profile)
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
                    kind="note",
                    scope_type="material",
                    scope_id=found.id,
                    exam_profile_id=profile.id if profile else None,
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
                "kind": "note",
                "material_id": found.id,
                "resend": open_task is not None,
                "exam_profile_id": profile.id if profile else None,
                "pages": len(task.payload["content"]),
                "pictures": len(task.pictures),
                "topics": len(task.payload["rules"]["topics"]),
                "past_exam_items": len(task.payload["rules"]["past_exam_items"]),
                "bytes": size,
            }
        )
    )
    return task


def _current_profile(conn: Connection, user_id: str, course_id: str):
    """The course's newest exam profile - its id and slot - or None on a cold start. I3302 has
    one profiled slot, so this is that slot's newest version; D-71 carries the two-slot case."""
    return conn.execute(
        select(exam_profile.c.id, exam_profile.c.slot_id)
        .join(assessment_slot, assessment_slot.c.id == exam_profile.c.slot_id)
        .where(assessment_slot.c.course_id == course_id, exam_profile.c.owner_id == user_id)
        .order_by(exam_profile.c.id.desc())  # ULIDs sort by time: the last derived first
        .limit(1)
    ).one_or_none()


def _note_task(conn: Connection, found, task_id: str, profile) -> Task:
    """The task for one material: its pages, the schema, and the rules - how to write the note,
    the profile's topic weights, and the past-exam items behind them (D-71, D-74). The items
    are the profile's paper set: its slot's transcribed papers (D-69)."""
    content, pictures = _pages(found.file_ref)

    weights = []
    items = []
    if profile is not None:
        rows = conn.execute(
            select(topic.c.id, topic.c.name, topic_weight.c.weight)
            .join(topic_weight, topic_weight.c.topic_id == topic.c.id)
            .where(topic_weight.c.exam_profile_id == profile.id, topic.c.status == "active")
            .order_by(topic_weight.c.weight.desc(), topic.c.name)
        )
        weights = [dict(r._mapping) for r in rows]
        rows = conn.execute(
            select(
                assessment_slot.c.name.label("slot"),
                past_exam.c.session_date,
                past_exam.c.session_type,
                practice_item.c.position,
                practice_item.c.marks,
                topic.c.name.label("topic"),
                practice_item.c.question,
            )
            .join_from(practice_item, past_exam, practice_item.c.past_exam_id == past_exam.c.id)
            .join(assessment_slot, assessment_slot.c.id == past_exam.c.slot_id)
            .join(topic, topic.c.id == practice_item.c.topic_id)
            .where(
                past_exam.c.slot_id == profile.slot_id,
                past_exam.c.transcript_task_id.is_not(None),
            )
            .order_by(past_exam.c.session_date, practice_item.c.position)
        )
        items = [
            {
                "paper": f"{r.slot} {r.session_date} ({r.session_type})",
                "position": r.position,
                "marks": r.marks,
                "topic": r.topic,
                "question": r.question,
            }
            for r in rows
        ]

    payload = {
        "task_id": task_id,
        "content": content,
        "schema": NOTE_SCHEMA,
        "rules": {"steps": NOTE_STEPS, "topics": weights, "past_exam_items": items},
    }
    return Task(payload=payload, pictures=pictures)


def _pages(file_ref: str) -> tuple[list[dict], list[Picture]]:
    """A kept file as `content` travels: each page's number and text, and `"image": n` when
    picture n (from 1, in `pictures`) shows that page whole (D-49). Shared by every kind that
    reads a file - a paper or a material."""
    content = []
    pictures = []
    for index, p in enumerate(intake.read_pages(file_ref), start=1):
        page = {}
        if p.image is not None:
            assert p.mime_type is not None  # intake sets it with every picture
            pictures.append(Picture(mime_type=p.mime_type, data=base64.b64encode(p.image).decode()))
            page["image"] = len(pictures)
        else:
            page["image"] = None
        page["page"] = index
        page["text"] = p.text
        content.append(page)
    return content, pictures


# --- inbound (1.10) -------------------------------------------------------------


@dataclass
class Judged:
    """One submission's items, sorted: `accepted` are the items to write, `errors` the item
    errors to return, `skipped` the positions already on the task (D-52)."""

    accepted: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    skipped: list[int] = field(default_factory=list)


def submit_generation(engine: Engine, user_id: str, task_id: str, payload: Any) -> dict:
    """Take one submission for an open task: keep what is valid, return the rest as item
    errors, and close the task when nothing is wrong or missing (D-11, D-50 - D-55).

    Returns the reply the host reads:
    {"task_id", "submission", "status", "accepted", "skipped", "missing", "item_errors",
     "submissions_left"} - a note's reply adds "exports", what the vault export did (D-72).
    """

    with write_unit(engine) as conn:
        task = _open_task(conn, user_id, task_id)
        if task.kind == "note":
            reply, event = _submit_note(conn, user_id, task, payload)
        else:
            reply, event = _submit_transcription(conn, user_id, task, payload)

    if task.kind == "note":
        # after the commit: a failed file write never rolls back a note (D-72)
        reply["exports"] = notes.export_notes(engine, user_id, task.id)
        event["exported"] = sum(1 for e in reply["exports"] if e["exported_path"] is not None)

    log.info(json.dumps({"event": "submit_generation", "task_id": task.id, **event}))
    return reply


def _submit_transcription(conn: Connection, user_id: str, task, payload: Any) -> tuple[dict, dict]:
    """One submission for a transcription task, inside the caller's write unit. Returns the
    reply and the fields of its log line."""
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
    ordinal = _next_ordinal(conn, task.id)
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
    received = _record_submission(
        conn, user_id, task, ordinal, payload, reply, len(judged.accepted), rejected
    )
    event = {
        "submission": ordinal,
        "status": status,
        "accepted": len(judged.accepted),
        "rejected": rejected,
        "skipped": len(judged.skipped),
        "bytes": received,
    }
    return reply, event


def _submit_note(conn: Connection, user_id: str, task, payload: Any) -> tuple[dict, dict]:
    """One submission for a note task, inside the caller's write unit (D-11, D-52, D-70, D-73).
    The envelope is checked once, then each entry alone: schema -> section 1 only (a split
    material is not built yet) -> a second copy of a section in this payload is an error, the
    first is kept -> a section already on the task is skipped, never rewritten. Each accepted
    entry is one `note`; never a topic, never an edge. Returns the reply - the transcription
    reply's keys, sections in place of positions - and its log line's fields."""
    on_task = set(
        conn.execute(select(note.c.section_ordinal).where(note.c.task_id == task.id))
        .scalars()
        .all()
    )

    accepted: list[dict] = []
    skipped: list[int] = []
    errors: list[dict] = []
    env_error = _envelope_error(payload, _NOTE_ENVELOPE_CHECK, "notes")
    if env_error is not None:
        errors.append(env_error)
    else:
        seen: set[int] = set()
        for place, entry in enumerate(payload["notes"], start=1):
            invalid = _schema_errors(place, entry, _NOTE_ENTRY_CHECK, NOTE_ENTRY_SCHEMA)
            if invalid:
                errors.extend(invalid)
            elif entry["section"] not in NOTE_SECTIONS:
                errors.append(
                    _item_error(
                        place,
                        "unknown_section",
                        "section",
                        f"this material has no section {entry['section']} - send section 1",
                    )
                )
            elif entry["section"] in seen:
                errors.append(
                    _item_error(
                        place,
                        "duplicate_section",
                        "section",
                        f"section {entry['section']} is already in this payload - the first "
                        "copy was kept",
                    )
                )
            elif entry["section"] in on_task:
                seen.add(entry["section"])
                skipped.append(entry["section"])
            else:
                seen.add(entry["section"])
                accepted.append(entry)

    for entry in accepted:
        conn.execute(
            insert(note).values(
                id=new_id(),
                user_id=user_id,
                material_id=task.scope_id,
                task_id=task.id,
                section_ordinal=entry["section"],
                exam_profile_id=task.exam_profile_id,
                body=entry["body"],
                created_at=now(),
            )
        )

    sections = {entry["section"] for entry in accepted}
    missing = sorted(NOTE_SECTIONS - on_task - sections)
    ordinal = _next_ordinal(conn, task.id)
    if not missing and not errors:
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

    reply = {
        "task_id": task.id,
        "submission": ordinal,
        "status": status,
        "accepted": sorted(sections),
        "skipped": sorted(skipped),
        "missing": missing,
        "item_errors": errors,
        "submissions_left": MAX_SUBMISSIONS - ordinal if status == "open" else 0,
    }
    # an envelope error names no entry, so it rejects none (D-52)
    rejected = len({e["item_ordinal"] for e in errors} - {None})
    received = _record_submission(
        conn, user_id, task, ordinal, payload, reply, len(accepted), rejected
    )
    event = {
        "submission": ordinal,
        "status": status,
        "accepted": len(accepted),
        "rejected": rejected,
        "skipped": len(skipped),
        "bytes": received,
    }
    return reply, event


def _next_ordinal(conn: Connection, task_id: str) -> int:
    """This submission's number on its task: 1, 2 or 3."""
    return (
        conn.execute(
            select(func.count()).select_from(submission).where(submission.c.task_id == task_id)
        ).scalar_one()
        + 1
    )


def _record_submission(
    conn: Connection,
    user_id: str,
    task,
    ordinal: int,
    payload: Any,
    reply: dict,
    accepted_count: int,
    rejected_count: int,
) -> int:
    """The `submission` row - the payload exactly as received, kept for the eval set (D-12) -
    and both byte counters on the task. Returns the bytes received."""
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
                    "ok": not reply["item_errors"],
                    "item_errors": reply["item_errors"],
                    "skipped": reply["skipped"],
                }
            ),
            accepted_count=accepted_count,
            rejected_count=rejected_count,
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
    return received


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


def _envelope_error(
    payload: Any, check: Draft202012Validator = _ENVELOPE_CHECK, key: str = "items"
) -> dict | None:
    """The payload's one envelope error - it is not {key: [a non-empty list]} - or None."""
    err = best_match(check.iter_errors(payload))
    if err is None:
        return None
    where = key if err.path or err.validator == "required" else "payload"
    return _item_error(None, str(err.validator), where, err.message)


TOPIC_SHAPE = 'topic must be {"id": <an id from rules.topics>} or {"proposed_name": <a name>}'


def _schema_errors(
    ordinal: int,
    item: Any,
    check: Draft202012Validator = _ITEM_CHECK,
    schema: dict = ITEM_SCHEMA,
) -> list[dict]:
    """Every way one item breaks its schema - a transcription item, or a note entry - as item
    errors: code = the failing keyword, field = the item field it is about (D-54)."""
    errors = []
    for err in check.iter_errors(item):
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
            extra = [k for k in err.instance if k not in schema["properties"]]
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
