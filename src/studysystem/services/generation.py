"""The generation contract, outbound (D-11, D-18, D-45 - D-49): the server hands the host a task -
`{task_id, part, parts, content, schema, rules}`, followed by a picture of each page that has an
image on it - and never calls a model itself. A task too big for one reply goes out in parts,
fetched in order (D-145). `study_submit_generation` (1.10) checks what comes back against
the same `schema`.

Five kinds are built, each for a PDF with a text layer - each page leaves as its text, plus a
picture when a figure sits on it (D-49); a scan or a set of photos waits for 2.6:
- `transcription` (1.9 - 1.10, D-50 - D-55): a past paper becomes `practice_item` rows.
- `note` (1.14, D-70 - D-74): a material becomes one `note`, aimed by the course's profile - its
  weights and the past-exam items behind them - then copied into the vault (`notes.py`, D-72).
- `tagging` (1.15b, D-93, D-102, D-104): a material is linked to the course's `active` topics it
  teaches, as `inferred` topic -> material coverage edges the scheduler reads (D-90, D-91).
- `observation` (2.1a, D-149, D-154): a past paper's format and instructor claims become
  `observation` rows, the input of claim-class routing (2.1b).
- `practice` (2.7, D-211): a material becomes new `generated` practice items, each on a topic with
  an `unverified` answer key and a stored novelty score.

Start writes one row, the task (D-45); starting again while it is open hands back a part of the
same task and adds it to its `bytes_sent` (D-46, D-145). Submit checks the envelope once, then
each item alone against the kind's schema, keeps the valid ones and returns the rest as item
errors - one write unit per submission, at most three per task.
"""

import base64
import hashlib
import json
import logging
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import best_match
from sqlalchemy import Connection, Engine, func, insert, select, update

from studysystem.db.tables import (
    assessment_slot,
    course,
    evidence,
    exam_profile,
    generation_task,
    material,
    note,
    observation,
    past_exam,
    practice_item,
    submission,
    topic,
    topic_weight,
)
from studysystem.errors import StudyError
from studysystem.services import claims, feedback, intake, notes, topics, values
from studysystem.services.ids import new_id, now
from studysystem.services.parts import Part, split
from studysystem.services.units import write_unit

# One JSON line per call, after its write commits - enough to answer "what went out?" without a
# debugger (build plan: logs, not tables).
log = logging.getLogger(__name__)

KINDS = ("note", "practice", "memory", "transcription", "timetable", "tagging", "observation")
BUILT = ("transcription", "note", "tagging", "observation", "practice")  # the rest arrive later

MAX_POSITION = 200  # no paper has this many questions (live maximum 24); `missing` walks 1..max

# What 1.10 validates each submission against. The server fills in what the host must not get
# wrong: origin = past-exam, past_exam_id, source_marker = from-material, answer_provenance.
TRANSCRIPTION_SCHEMA = {
    "type": "object",
    "required": ["items"],
    "additionalProperties": False,
    "properties": {
        "header": {
            "type": "object",
            "additionalProperties": False,
            "description": "scanned paper only: what page 1 prints, null for what it does not",
            "properties": {
                "date": {"type": ["string", "null"]},
                "course": {"type": ["string", "null"]},
                "instructor": {"type": ["string", "null"]},
            },
        },
        "items": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["position", "question", "topic"],
                "additionalProperties": False,
                "properties": {
                    "page": {
                        "type": "integer",
                        "minimum": 1,
                        "description": "scanned paper only: the page the question is on",
                    },
                    "position": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": MAX_POSITION,
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
        },
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
    "A scanned paper (pages with a picture and no text): give each item the page it is on, and "
    "send header with what page 1 prints - date, course, instructor, null for any it does not.",
    "Submit with study_submit_generation(task_id, payload), payload matching schema.",
]

# Inbound (D-54): the envelope is checked once, then each item alone against its own sub-schema,
# so one bad item fails alone. Both come from TRANSCRIPTION_SCHEMA - the schema the host read.
ITEM_SCHEMA = TRANSCRIPTION_SCHEMA["properties"]["items"]["items"]
ENVELOPE_SCHEMA = {
    **TRANSCRIPTION_SCHEMA,
    "properties": {
        "items": {"type": "array", "minItems": 1},
        "header": TRANSCRIPTION_SCHEMA["properties"]["header"],
    },
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
# everything it needs to aim the note travels in `rules`. Dropped and changed rows: D-74. Flow in
# the pages' order and the visuals rule: D-148.
NOTE_STEPS = [
    "Write one exam-ready study note for this material, aimed by the exam evidence in rules - "
    "not by how much space the pages give each part.",
    "Flow: Layer 1 and Layer 2 follow the order of the pages, start to end. Exam weight decides "
    "how deep each part goes and which parts get 🎯, never the order - a part with little "
    "weight may be what a heavier part builds on.",
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
    "Layer 1 - Summary: the whole material compressed for a night-before read, in the pages' "
    "order, 700-900 words at most. Compress; never repeat Layer 2 word for word.",
    "Layer 2 - Core notes, in the pages' order: definitions, syntax, functions and algorithms, "
    "each with a worked example, as a strong student takes them in class. If you can run code, "
    "run every worked example and show its real output.",
    "Visuals: where a page's picture shows a diagram - a figure, a layout with its parts, steps "
    "in order, a comparison table - redraw it in the note where its topic is taught, and cite "
    "the page. Also draw one where words alone are hard to follow: steps in order, a layout "
    "with its parts, a side-by-side comparison. Draw only with markdown tables and with "
    "characters inside a code block - no images, HTML or Mermaid - so they print in the PDF as "
    "written; keep each under 90 characters wide. Never put 🎯 inside a table or a diagram. No "
    "visual for decoration.",
    "Layer 3 - Depth & pitfalls: edge cases and tricky details, only where an exam could ask them.",
    "Markers: 🎯 likely exam target - justify each from rules, citing the item or the weight; "
    "💡 tip or shortcut; ⚠️ common mistake. There is no mistake history yet, so say once that the "
    "⚠️ items are generic.",
    "Order: a title; the legend line '🎯 likely exam target · 💡 tip · ⚠️ common mistake'; Exam "
    "positioning; Layer 1; Layer 2; Layer 3; To-Memorize; Self-check - 3 to 5 recall prompts, "
    "no answers. No frontmatter: the server adds it.",
    "rules.active_directives are standing instructions the student confirmed: follow each one. "
    "Its scope says where it applies: topic - only the parts of the note on the topic named in "
    "applies_to; course or all - the whole note. Skip one that cannot apply to a note, such as "
    "a rule about practice questions.",
    "Submit with study_submit_generation(task_id, payload): "
    '{"notes": [{"section": 1, "body": the whole note as markdown}]}.',
]


# A tagging task's answer (1.15b, D-93, D-102): the ids of the topics the material teaches. No
# minItems - a material can teach none of them, and `[]` closes the task with no edge (D-102).
TAGGING_SCHEMA = {
    "type": "object",
    "required": ["topics"],
    "additionalProperties": False,
    "properties": {
        "topics": {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["id"],
                "additionalProperties": False,
                "properties": {
                    "id": {
                        "type": "string",
                        "description": "the id of a topic in rules.topics this material teaches",
                    }
                },
            },
        }
    },
}

# Inbound, as for the other kinds (D-54): the envelope once, then each entry alone.
TAGGING_ENTRY_SCHEMA = TAGGING_SCHEMA["properties"]["topics"]["items"]
_TAGGING_ENTRY_CHECK = Draft202012Validator(TAGGING_ENTRY_SCHEMA)
_TAGGING_ENVELOPE_CHECK = Draft202012Validator(
    {**TAGGING_SCHEMA, "properties": {"topics": {"type": "array"}}}
)

# How to decide (D-104): a topic is its past-exam questions, not its name, and "teaches" means
# he could implement a part of one of them after studying this material.
TAGGING_STEPS = [
    "Decide which topics in rules.topics this material teaches. Read every page first.",
    "A topic is defined by its past-exam questions in rules.topics, not by its name alone: "
    "compare the material with those questions.",
    "The material teaches a topic when studying it would let the student implement at least one "
    "part of one of that topic's questions. A topic the material only uses, without teaching "
    "how, is not taught.",
    "Use only ids from rules.topics. A topic the material teaches that is not in rules.topics "
    "is left out - never invent one.",
    "If the material teaches none of them, send an empty list.",
    "Submit with study_submit_generation(task_id, payload): "
    '{"topics": [{"id": an id from rules.topics}, ...]}.',
]


# An observation task's answer (2.1a, D-149, D-154): one paper's format and instructor claims.
# Each key fixes its value's shape; `topic-marks` is not offered - weights come from the
# transcribed questions (D-151).
CLAIM_VALUE_SCHEMAS = {
    "duration-minutes": {
        "type": "object",
        "required": ["minutes"],
        "additionalProperties": False,
        "properties": {"minutes": {"type": "integer", "minimum": 1, "maximum": 600}},
    },
    "total-marks": {
        "type": "object",
        "required": ["marks"],
        "additionalProperties": False,
        "properties": {"marks": {"type": "number", "exclusiveMinimum": 0, "maximum": 1000}},
    },
    "question-count": {
        "type": "object",
        "required": ["count"],
        "additionalProperties": False,
        "properties": {"count": {"type": "integer", "minimum": 1, "maximum": MAX_POSITION}},
    },
    "has-mcq": {
        "type": "object",
        "required": ["value"],
        "additionalProperties": False,
        "properties": {"value": {"type": "boolean"}},
    },
    **{
        key: {
            "type": "object",
            "required": ["text"],
            "additionalProperties": False,
            "properties": {"text": {"type": "string", "pattern": r"\S"}},
        }
        for key in ("archetype", "phrasing", "trap")
    },
}
OBSERVATION_SCHEMA = {
    "type": "object",
    "required": ["claims"],
    "additionalProperties": False,
    "properties": {
        "claims": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["claim_key", "claim_value"],
                "additionalProperties": False,
                "properties": {
                    "claim_key": {"enum": list(CLAIM_VALUE_SCHEMAS)},
                    "claim_value": {
                        "type": "object",
                        "description": "its shape is fixed by claim_key",
                    },
                    "citation": {
                        "type": "string",
                        "pattern": r"\S",
                        "description": "where on the paper: 'header', 'Q3b'",
                    },
                },
                "allOf": [
                    {
                        "if": {"properties": {"claim_key": {"const": key}}},
                        "then": {"properties": {"claim_value": shape}},
                    }
                    for key, shape in CLAIM_VALUE_SCHEMAS.items()
                ],
            },
        }
    },
}

# Inbound, as for the other kinds (D-54): the envelope once, then each claim alone.
CLAIM_SCHEMA = OBSERVATION_SCHEMA["properties"]["claims"]["items"]
_CLAIM_CHECK = Draft202012Validator(CLAIM_SCHEMA)
_OBSERVATION_ENVELOPE_CHECK = Draft202012Validator(
    {**OBSERVATION_SCHEMA, "properties": {"claims": {"type": "array", "minItems": 1}}}
)

OBSERVATION_STEPS = [
    "Read every page of this past exam paper and record what it shows about the exam, as claims.",
    "Format claims - only what the paper itself prints or shows; when it does not show one, leave "
    "that claim out, never guess: duration-minutes (the printed duration, in minutes), "
    "total-marks (the printed total), question-count (the numbered questions, counted), has-mcq "
    "(true when any question is multiple choice, false when none is). Each at most once.",
    "Instructor claims - how this paper examines, one claim per distinct point: archetype (a "
    "kind of task the paper sets, e.g. 'write a script that validates a form and stores it with "
    "a prepared statement'), phrasing (a wording habit of its questions), trap (a detail that "
    "catches a student who knows the topic only roughly). Several of each are fine; never send "
    "the same one twice - a point seen on several questions is one claim citing them all.",
    "citation: where on the paper the claim comes from - 'header', 'Q2', 'Q3b', or 'Q2, Q4' "
    "for a point seen on both.",
    "Never send topic-marks: topic weights come from the transcribed questions.",
    "Submit with study_submit_generation(task_id, payload): "
    '{"claims": [{"claim_key": ..., "claim_value": {...}, "citation": ...}, ...]}.',
]

# A practice task's answer (2.7, D-211): new questions on the course's topics. The server fills in
# what the host must not get wrong: origin = generated, answer_provenance = unverified (a key the
# host wrote is never verified, D-16), state, novelty_score. No provenance field - one sent is
# an `additionalProperties` error for that item.
PRACTICE_SCHEMA = {
    "type": "object",
    "required": ["items"],
    "additionalProperties": False,
    "properties": {
        "items": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "required": ["question", "answer_key", "topic_id", "source"],
                "additionalProperties": False,
                "properties": {
                    "question": {
                        "type": "string",
                        "pattern": r"\S",
                        "description": "one new question, with all a student needs to answer it",
                    },
                    "answer_key": {
                        "type": "string",
                        "pattern": r"\S",
                        "description": "the answer you would mark against - every item needs one",
                    },
                    "topic_id": {
                        "type": "string",
                        "description": "the id of one topic in rules.topics",
                    },
                    "source": {
                        "enum": ["from-material", "model-knowledge"],
                        "description": "from-material: the pages teach what it asks; "
                        "model-knowledge: it needs more than the pages",
                    },
                },
            },
        },
    },
}
PRACTICE_ITEM_SCHEMA = PRACTICE_SCHEMA["properties"]["items"]["items"]
_PRACTICE_ITEM_CHECK = Draft202012Validator(PRACTICE_ITEM_SCHEMA)
_PRACTICE_ENVELOPE_CHECK = Draft202012Validator(
    {**PRACTICE_SCHEMA, "properties": {"items": {"type": "array", "minItems": 1}}}
)

PRACTICE_STEPS = [
    "Write new practice questions from this material, each on one topic in rules.topics. Read "
    "every page first.",
    "Aim at what the course's exams ask: the questions in rules.topics' past_exam_questions "
    "show the style. Never copy one - write a new question on the same ground.",
    "Every item needs question, answer_key, topic_id and source. The answer_key is the answer you "
    "would mark against; it is stored as unverified, so the student sees it is yours.",
    "topic_id: an id from rules.topics, exactly as given.",
    "source: from-material when the pages teach what the question asks, model-knowledge when it "
    "needs more than the pages. Be honest - it is never checked, only counted.",
    "When rules.count is set, write that many items. Items past it are dropped.",
    "rules.active_directives are standing instructions the student confirmed: follow each one. "
    "Its scope says where it applies: topic - only items on the topic named in applies_to; "
    "course or all - every item. Skip one that cannot apply to a question, such as a rule "
    "about notes.",
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


# --- parts (D-145) ---------------------------------------------------------------------------

# Room in part 1's text for the "part" and "parts" fields the split's head does not see.
PART_FIELDS = len(', "part": 999, "parts": 999')


@dataclass(frozen=True)
class Layout:
    """A whole task cut into parts: `split`'s answer, kept with the task it was cut from so any
    part can be built from it. `extras_at` names the long list that pages alongside the pages -
    ("rules", "topics") for a tagging - and `keep` the fields every later part repeats so the
    host knows what it belongs to."""

    whole: Task
    parts: list[Part]
    extras_at: tuple[str, str] | None
    keep: tuple[str, ...]
    layout_hash: str

    def build(self, part: int) -> Task:
        """Part `part` (from 1) as it travels: its pages, their pictures numbered from 1 within
        this reply, and its slice of the long list. Part 1 carries everything else as well."""
        chosen = self.parts[part - 1]
        pictures = []
        content = []
        for i in chosen.pages:
            page = dict(self.whole.payload["content"][i])
            if page["image"] is not None:
                pictures.append(self.whole.pictures[page["image"] - 1])
                page["image"] = len(pictures)
            content.append(page)

        whole = self.whole.payload
        payload = {k: whole[k] for k in self.keep} | {"part": part, "parts": len(self.parts)}
        if part == 1:
            payload |= {k: v for k, v in whole.items() if k not in self.keep}
        payload["content"] = content
        if self.extras_at is not None:
            outer, inner = self.extras_at
            entries = [whole[outer][inner][j] for j in chosen.extras]
            payload[outer] = (dict(whole[outer]) if part == 1 else {}) | {inner: entries}
        return Task(payload=payload, pictures=pictures)


def layout(whole: Task, extras_at: tuple[str, str] | None, keep: tuple[str, ...]) -> Layout:
    """Cut a whole task into parts under the measured budget (D-145), sizes as they travel: a
    page is its JSON plus its picture's base64, an entry its JSON. The hash covers every byte of
    the whole task, so any change to the content - a topic accepted, a question rejected -
    changes it."""
    payload = whole.payload
    extras = payload[extras_at[0]][extras_at[1]] if extras_at is not None else []
    bare = payload | {"content": []}
    if extras_at is not None:
        outer, inner = extras_at
        bare[outer] = payload[outer] | {inner: []}
    pages = [
        (
            len(json.dumps(p)),
            len(whole.pictures[p["image"] - 1].data) if p["image"] is not None else 0,
        )
        for p in payload["content"]
    ]
    head = len(json.dumps(bare)) + PART_FIELDS
    digest = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8"))
    for p in whole.pictures:
        digest.update(p.data.encode("ascii"))
    return Layout(
        whole=whole,
        parts=split(head, pages, [len(json.dumps(e)) for e in extras]),
        extras_at=extras_at,
        keep=keep,
        layout_hash=digest.hexdigest(),
    )


def part_number(part: object) -> int:
    """`part` as a whole number from 1."""
    nb = values.number("part", part, ge=1)
    if nb != int(nb):
        raise values.invalid("part", f"{nb:g} is not a whole number", "send 1, 2, 3...")
    return int(nb)


def in_range(n: int, cut: Layout) -> None:
    """Refuse a part the layout does not have."""
    if n > len(cut.parts):
        raise values.invalid(
            "part",
            f"there is no part {n} - this task has {len(cut.parts)}",
            f"send a part from 1 to {len(cut.parts)}",
        )


def _send(conn: Connection, open_task, cut: Layout, part: object, row: dict) -> tuple[Task, int]:
    """Record one part going out and return it with its size. A new task is written with its
    layout; an open one takes the parts in order - part 1 any time, part n once n - 1 went out -
    and refuses a later part cut from changed content, which part 1 restarts (D-145)."""
    n = part_number(part)
    changed = open_task is not None and cut.layout_hash != open_task.layout_hash
    if open_task is None and n != 1:
        raise values.invalid(
            "part", "this task has not started yet", "fetch part 1 first, then the others"
        )
    if n != 1 and changed:
        raise StudyError(
            code="task_changed",
            message="the task's content changed since part 1 went out, so its parts moved",
            fix="fetch part 1 again, then every part after it in order",
            field_errors=[{"field": "part", "problem": "cut from content that has changed"}],
        )
    in_range(n, cut)
    if open_task is not None and n > open_task.parts_sent + 1:
        raise StudyError(
            code="part_out_of_order",
            message=f"part {n} asked before part {open_task.parts_sent + 1}",
            fix=f"fetch part {open_task.parts_sent + 1} next - parts go out in order",
            field_errors=[{"field": "part", "problem": "an earlier part was not fetched"}],
        )
    sent = cut.build(n)
    size = measure(sent.payload, sent.pictures)

    if open_task is None:
        conn.execute(
            insert(generation_task).values(
                **row,
                route="host",
                parts=len(cut.parts),
                parts_sent=1,
                layout_hash=cut.layout_hash,
                bytes_sent=size,
                created_at=now(),
            )
        )
        return sent, size

    changes: dict = {"bytes_sent": generation_task.c.bytes_sent + size}
    if changed:  # only part 1 gets here changed: it restarts the count on the new layout
        changes |= {"parts": len(cut.parts), "parts_sent": 1, "layout_hash": cut.layout_hash}
    else:
        changes["parts_sent"] = max(open_task.parts_sent, n)
    conn.execute(
        update(generation_task).where(generation_task.c.id == open_task.id).values(**changes)
    )
    return sent, size


def start_generation(
    engine: Engine,
    user_id: str,
    kind: str,
    past_exam_id: str | None = None,
    material_id: str | None = None,
    part: object = 1,
    count: object = None,
) -> Task:
    """Hand the host part `part` of a generation task: the payload {"task_id", "part", "parts",
    "content", "schema", "rules"} and the page pictures that follow it - later parts carry only
    their pages and their slice of the long list (D-145). Each kind takes the parameter named
    for its subject (D-47): `past_exam_id` for a transcription or an observation, `material_id`
    for a note, a tagging or a practice. `count` is a practice's: how many items to keep."""

    kind = values.choice("kind", kind, KINDS)
    if kind not in BUILT:
        raise StudyError(
            code="not_supported_yet",
            message=f"{kind} generation is not built yet",
            fix=f"only these kinds are available now: {', '.join(BUILT)}",
            field_errors=[{"field": "kind", "problem": "not available yet"}],
        )
    if kind == "note":
        return _start_note(engine, user_id, material_id, part)
    if kind == "tagging":
        return _start_tagging(engine, user_id, material_id, part)
    if kind == "practice":
        return _start_practice(engine, user_id, material_id, count, part)
    if kind == "observation":
        return _start_observation(engine, user_id, past_exam_id, part)
    return _start_transcription(engine, user_id, kind, past_exam_id, part)


def _start_transcription(
    engine: Engine, user_id: str, kind: str, past_exam_id: str | None, part: object
) -> Task:
    """One paper's transcription task, written alone - or the same open task again (D-45, D-46)."""
    with write_unit(engine) as conn:
        paper = _find_paper(conn, user_id, past_exam_id)
        if paper.transcript_task_id is not None and paper.transcript_confirmed_at is None:
            raise StudyError(
                code="already_transcribed",
                message="this scan is transcribed and waits for the student to confirm it",
                fix="compare its read_back with the paper: if it is right, "
                "study_confirm_transcription; if it is wrong, leave it unconfirmed - it does not "
                "count, and redoing it arrives with study_rescope_generation (D-210)",
                field_errors=[{"field": "past_exam_id", "problem": "transcribed, not confirmed"}],
            )
        if paper.transcript_task_id is not None:
            raise StudyError(
                code="already_transcribed",
                message="this paper is already transcribed",
                fix="nothing to do - its questions are already in the pool",
                field_errors=[{"field": "past_exam_id", "problem": "already transcribed"}],
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
        cut = layout(task, ("rules", "topics"), ("task_id",))
        row = dict(
            id=task_id,
            user_id=user_id,
            kind="transcription",
            scope_type="past-exam",
            scope_id=paper.id,
        )
        sent, size = _send(conn, open_task, cut, part, row)

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
                "part": sent.payload["part"],
                "parts": sent.payload["parts"],
                "bytes": size,
            }
        )
    )
    return sent


def _start_observation(
    engine: Engine, user_id: str, past_exam_id: str | None, part: object
) -> Task:
    """One paper's observation task, written alone - or the same open task again, even after it
    kept claims, so a lost payload can be fetched (D-46, D-149). With no open task, a paper with
    an `observation` row is refused: rows are insert-only, so a second task would add every claim
    again (D-154)."""
    with write_unit(engine) as conn:
        paper = _find_paper(conn, user_id, past_exam_id)
        open_task = conn.execute(
            select(generation_task).where(
                generation_task.c.user_id == user_id,
                generation_task.c.kind == "observation",
                generation_task.c.scope_type == "past-exam",
                generation_task.c.scope_id == paper.id,
                generation_task.c.status == "open",
            )
        ).one_or_none()
        if open_task is None:
            kept = conn.execute(
                select(func.count())
                .select_from(observation)
                .where(observation.c.past_exam_id == paper.id)
            ).scalar_one()
            if kept:
                raise StudyError(
                    code="already_observed",
                    message=f"this paper already holds {kept} observations",
                    fix="nothing to do - its claims are already stored",
                    field_errors=[{"field": "past_exam_id", "problem": "already observed"}],
                )
        _refuse_scan(paper)
        task_id = open_task.id if open_task else new_id()
        content, pictures = _pages(paper.file_ref)
        task = Task(
            payload={
                "task_id": task_id,
                "content": content,
                "schema": OBSERVATION_SCHEMA,
                "rules": {"steps": OBSERVATION_STEPS},
            },
            pictures=pictures,
        )
        cut = layout(task, None, ("task_id",))
        row = dict(
            id=task_id,
            user_id=user_id,
            kind="observation",
            scope_type="past-exam",
            scope_id=paper.id,
            exam_profile_id=None,  # a claim is about one paper, not a ranking
        )
        sent, size = _send(conn, open_task, cut, part, row)

    log.info(
        json.dumps(
            {
                "event": "start_generation",
                "task_id": task_id,
                "kind": "observation",
                "past_exam_id": paper.id,
                "resend": open_task is not None,
                "pages": len(task.payload["content"]),
                "pictures": len(task.pictures),
                "part": sent.payload["part"],
                "parts": sent.payload["parts"],
                "bytes": size,
            }
        )
    )
    return sent


def _find_paper(conn: Connection, user_id: str, past_exam_id: str | None):
    """This user's paper `past_exam_id`, or the refusal: missing or not found."""
    if past_exam_id is None:
        raise values.invalid(
            "past_exam_id", "is missing", "send the past_exam_id that study_add_past_exam returned"
        )
    paper = conn.execute(
        select(past_exam).where(past_exam.c.id == past_exam_id, past_exam.c.owner_id == user_id)
    ).one_or_none()
    if paper is None:
        raise StudyError(
            code="not_found",
            message=f"no past exam {past_exam_id}",
            fix="use a past_exam_id that study_add_past_exam returned",
            field_errors=[{"field": "past_exam_id", "problem": "no such paper"}],
        )
    return paper


def confirm_transcription(engine: Engine, user_id: str, past_exam_id: str | None) -> dict:
    """He compared a scan's read_back with the paper: stamp `transcript_confirmed_at`, and from
    now on derive and the note task count the paper (D-210). One row, in a write unit.

    Returns {"past_exam_id", "transcript_confirmed_at"}."""
    with write_unit(engine) as conn:
        paper = _find_paper(conn, user_id, past_exam_id)
        if paper.transcript_task_id is None:
            raise StudyError(
                code="not_transcribed",
                message="this paper has no transcription to confirm",
                fix="transcribe it first: study_start_generation(kind='transcription', "
                "past_exam_id), then study_submit_generation",
                field_errors=[{"field": "past_exam_id", "problem": "not transcribed"}],
            )

        if paper.transcript_confirmed_at is not None:
            raise StudyError(
                code="already_confirmed",
                message=f"this paper was confirmed at {paper.transcript_confirmed_at}",
                fix="nothing to do - it already counts toward the exam profile",
                field_errors=[{"field": "past_exam_id", "problem": "already confirmed"}],
            )

        stamp = now()  # one value: the reply says exactly what the row holds
        conn.execute(
            update(past_exam)
            .where(past_exam.c.id == paper.id)
            .values(transcript_confirmed_at=stamp)
        )

    return {"past_exam_id": paper.id, "transcript_confirmed_at": stamp}


def _refuse_scan(paper) -> None:
    """An observation still waits for the image path; a transcription reads a scan (D-210)."""
    if paper.has_text_layer == 0:
        raise StudyError(
            code="not_supported_yet",
            message="this paper is a scan with no text layer",
            fix="an observation task cannot read a scan's page images yet - transcribe it instead",
            field_errors=[{"field": "past_exam_id", "problem": "no text layer"}],
        )


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


def _start_note(engine: Engine, user_id: str, material_id: str | None, part: object) -> Task:
    """One material's note task, written alone - or the same open task again, even after it
    kept a note, so a lost payload can be fetched (D-46, D-71, D-73, D-74, D-155). With no open
    task, a material with a `note` row is refused: a task can keep a note and still end
    `partial` or `invalidated` (section 1 accepted, section 2 an error). A task that kept no
    note lets the material start again (D-76)."""
    if material_id is None:
        raise values.invalid(
            "material_id", "is missing", "send the material_id that study_add_material returned"
        )

    with write_unit(engine) as conn:
        found = _find_material(conn, user_id, material_id)
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
        if open_task is None:
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

        task_id = open_task.id if open_task else new_id()
        profile = _current_profile(conn, user_id, found.course_id)
        # Built before any write: a missing file copy fails here, with nothing written.
        task = _note_task(conn, found, task_id, profile)
        row = dict(
            id=task_id,
            user_id=user_id,
            kind="note",
            scope_type="material",
            scope_id=found.id,
            exam_profile_id=profile.id if profile else None,
        )
        cut = layout(task, ("rules", "past_exam_items"), ("task_id",))
        sent, size = _send(conn, open_task, cut, part, row)

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
                "directives": len(task.payload["rules"]["active_directives"]),
                "part": sent.payload["part"],
                "parts": sent.payload["parts"],
                "bytes": size,
            }
        )
    )
    return sent


def _find_material(conn: Connection, user_id: str, material_id: str):
    """The user's material a note or a tagging is about - refused when there is none, or when
    it is a scan whose pages have no text yet."""
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
    return found


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
                past_exam.c.transcript_confirmed_at.is_not(None),  # counted: stamped (D-210)
                practice_item.c.state == "live",
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
        "rules": {
            "steps": NOTE_STEPS,
            "topics": weights,
            "past_exam_items": items,
            "active_directives": feedback.active_directives(conn, found.user_id, found.course_id),
        },
    }
    return Task(payload=payload, pictures=pictures)


def _start_tagging(engine: Engine, user_id: str, material_id: str | None, part: object) -> Task:
    """One material's tagging task, written alone - or the same open task again (D-46, D-93).
    A material tagged before is not refused: a re-run sends the course's topics as they are now,
    and its submit links only the pairs still missing (D-102). A course with no `active` topic
    is refused - there is nothing to link the material to."""
    if material_id is None:
        raise values.invalid(
            "material_id", "is missing", "send the material_id that study_add_material returned"
        )

    with write_unit(engine) as conn:
        found = _find_material(conn, user_id, material_id)
        active = conn.execute(
            select(func.count(topic.c.id)).where(
                topic.c.course_id == found.course_id, topic.c.status == "active"
            )
        ).scalar_one()
        if active == 0:
            raise StudyError(
                code="no_active_topics",
                message=f"the course of {found.filename} has no active topic to link it to",
                fix="transcribe the course's past papers (kind 'transcription') and accept "
                "their topics first, then tag this material",
                field_errors=[{"field": "material_id", "problem": "course has no active topic"}],
            )
        # At most one open tagging per material, as for notes: `one_or_none` raises on two.
        open_task = conn.execute(
            select(generation_task).where(
                generation_task.c.user_id == user_id,
                generation_task.c.kind == "tagging",
                generation_task.c.scope_type == "material",
                generation_task.c.scope_id == found.id,
                generation_task.c.status == "open",
            )
        ).one_or_none()

        task_id = open_task.id if open_task else new_id()
        # Built before any write: a missing file copy fails here, with nothing written.
        task = _tagging_task(conn, found, task_id)
        row = dict(
            id=task_id,
            user_id=user_id,
            kind="tagging",
            scope_type="material",
            scope_id=found.id,
            exam_profile_id=None,  # a tagging reads no weights (D-104)
        )
        cut = layout(task, ("rules", "topics"), ("task_id",))
        sent, size = _send(conn, open_task, cut, part, row)

    topics_sent = task.payload["rules"]["topics"]
    log.info(
        json.dumps(
            {
                "event": "start_generation",
                "task_id": task_id,
                "kind": "tagging",
                "material_id": found.id,
                "resend": open_task is not None,
                "pages": len(task.payload["content"]),
                "pictures": len(task.pictures),
                "topics": len(topics_sent),
                "questions": sum(len(t["past_exam_questions"]) for t in topics_sent),
                "part": sent.payload["part"],
                "parts": sent.payload["parts"],
                "bytes": size,
            }
        )
    )
    return sent


def _start_practice(
    engine: Engine, user_id: str, material_id: str | None, count: object, part: object
) -> Task:
    """One material's practice task, written alone - or the same open task again, which keeps the
    count it started with (D-211). A material may start many: each run is more questions. A
    course with no `active` topic is refused - an item has no topic to land on."""
    if material_id is None:
        raise values.invalid(
            "material_id", "is missing", "send the material_id that study_add_material returned"
        )
    wanted = None if count is None else _whole_number("count", count)

    with write_unit(engine) as conn:
        found = _find_material(conn, user_id, material_id)
        open_task = conn.execute(
            select(generation_task).where(
                generation_task.c.user_id == user_id,
                generation_task.c.kind == "practice",
                generation_task.c.scope_type == "material",
                generation_task.c.scope_id == found.id,
                generation_task.c.status == "open",
            )
        ).one_or_none()
        item_count = open_task.item_count if open_task else wanted

        task_id = open_task.id if open_task else new_id()
        # Built before any write: a missing file copy fails here, with nothing written.
        task = _practice_task(conn, found, task_id, item_count)
        if not task.payload["rules"]["topics"]:
            raise StudyError(
                code="no_active_topics",
                message=f"the course of {found.filename} has no active topic to write items on",
                fix="transcribe the course's past papers (kind 'transcription') and accept "
                "their topics first, then ask for practice",
                field_errors=[{"field": "material_id", "problem": "course has no active topic"}],
            )
        row = dict(
            id=task_id,
            user_id=user_id,
            kind="practice",
            scope_type="material",
            scope_id=found.id,
            exam_profile_id=None,  # a practice item is aimed by topic, not by weight
            item_count=item_count,
        )
        cut = layout(task, ("rules", "topics"), ("task_id",))
        sent, size = _send(conn, open_task, cut, part, row)

    log.info(
        json.dumps(
            {
                "event": "start_generation",
                "task_id": task_id,
                "kind": "practice",
                "material_id": found.id,
                "resend": open_task is not None,
                "pages": len(task.payload["content"]),
                "pictures": len(task.pictures),
                "topics": len(task.payload["rules"]["topics"]),
                "count": item_count,
                "directives": len(task.payload["rules"]["active_directives"]),
                "part": sent.payload["part"],
                "parts": sent.payload["parts"],
                "bytes": size,
            }
        )
    )
    return sent


def _whole_number(name: str, raw: object) -> int:
    """`raw` as a whole number from 1, or the structured error for it."""
    nb = values.number(name, raw, ge=1)
    if nb != int(nb):
        raise values.invalid(name, f"{nb:g} is not a whole number", "send 1, 2, 3...")
    return int(nb)


def _practice_task(conn: Connection, found, task_id: str, item_count: int | None) -> Task:
    """The task for one material: its pages, the schema, and the rules - how to write the items,
    the course's `active` topics with their past-exam questions, the count when one was asked
    for, and the directives he confirmed for the course (D-214)."""
    content, pictures = _pages(found.file_ref)
    rules: dict = {"steps": PRACTICE_STEPS, "topics": _topics_with_questions(conn, found.course_id)}
    if item_count is not None:
        rules["count"] = item_count
    rules["active_directives"] = feedback.active_directives(conn, found.user_id, found.course_id)
    payload = {"task_id": task_id, "content": content, "schema": PRACTICE_SCHEMA, "rules": rules}
    return Task(payload=payload, pictures=pictures)


def _tagging_task(conn: Connection, found, task_id: str) -> Task:
    """The task for one material: its pages, the schema, and the rules - how to decide, and each
    `active` topic of its course with every live past-exam question tagged to it, which is what
    defines the topic (D-104; a rejected question is not evidence, D-136). No weight: tagging is
    a yes or no per topic, not a ranking."""
    content, pictures = _pages(found.file_ref)
    payload = {
        "task_id": task_id,
        "content": content,
        "schema": TAGGING_SCHEMA,
        "rules": {"steps": TAGGING_STEPS, "topics": _topics_with_questions(conn, found.course_id)},
    }
    return Task(payload=payload, pictures=pictures)


def _topics_with_questions(conn: Connection, course_id: str) -> list[dict]:
    """The course's `active` topics, each with every live past-exam question tagged to it - what
    defines the topic (D-104; a rejected question is not evidence, D-136). Shared by a tagging
    and a practice task."""
    rows = conn.execute(
        select(topic.c.id, topic.c.name)
        .where(topic.c.course_id == course_id, topic.c.status == "active")
        .order_by(topic.c.id)
    )
    # One entry per topic, filled with its questions below; a topic with none keeps [].
    entries = {r.id: {"id": r.id, "name": r.name, "past_exam_questions": []} for r in rows}
    questions = conn.execute(
        select(
            practice_item.c.topic_id,
            assessment_slot.c.name.label("slot"),
            past_exam.c.session_date,
            past_exam.c.session_type,
            practice_item.c.position,
            practice_item.c.question,
        )
        .join_from(practice_item, past_exam, practice_item.c.past_exam_id == past_exam.c.id)
        .join(assessment_slot, assessment_slot.c.id == past_exam.c.slot_id)
        .where(practice_item.c.topic_id.in_(list(entries)), practice_item.c.state == "live")
        .order_by(past_exam.c.session_date, practice_item.c.position)
    )
    for q in questions:
        entries[q.topic_id]["past_exam_questions"].append(
            {
                "paper": f"{q.slot} {q.session_date} ({q.session_type})",
                "position": q.position,
                "question": q.question,
            }
        )
    return list(entries.values())


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
        if task.parts_sent < task.parts:
            # Refused before it counts as a submission: a skipped part is pages never read (D-145).
            missing = list(range(task.parts_sent + 1, task.parts + 1))
            raise StudyError(
                code="parts_missing",
                message=f"part {', '.join(map(str, missing))} of {task.parts} never went out",
                fix=f"fetch part {missing[0]} with study_start_generation(part={missing[0]}), "
                "then the rest in order, and submit after reading them all",
                field_errors=[{"field": "task_id", "problem": f"parts {missing} not fetched"}],
            )
        if task.kind == "note":
            reply, event = _submit_note(conn, user_id, task, payload)
        elif task.kind == "tagging":
            reply, event = _submit_tagging(conn, user_id, task, payload)
        elif task.kind == "practice":
            reply, event = _submit_practice(conn, user_id, task, payload)
        elif task.kind == "observation":
            reply, event = _submit_observation(conn, user_id, task, payload)
        else:
            reply, event = _submit_transcription(conn, user_id, task, payload)

    if task.kind == "note":
        # after the commit: a failed file write never rolls back a note (D-72)
        reply["exports"] = notes.export_notes(engine, user_id, task.id)
        event["exported"] = sum(1 for e in reply["exports"] if e["exported_path"] is not None)
        event["pdf_exported"] = sum(1 for e in reply["exports"] if e["pdf_path"] is not None)

    log.info(json.dumps({"event": "submit_generation", "task_id": task.id, **event}))
    return reply


def _submit_transcription(conn: Connection, user_id: str, task, payload: Any) -> tuple[dict, dict]:
    """One submission for a transcription task, inside the caller's write unit. Returns the
    reply and the fields of its log line."""
    # the whole row: a scan (D-210) reads its text layer, its file and its registered header
    paper = conn.execute(
        select(past_exam, assessment_slot.c.course_id, course.c.code)
        .join_from(past_exam, assessment_slot)
        .join(course, course.c.id == assessment_slot.c.course_id)
        .where(past_exam.c.id == task.scope_id)
    ).one()
    course_id = paper.course_id
    scan = paper.has_text_layer == 0
    # counted, never drawn - submit sends no picture out; a typed paper's page is never kept
    pages = intake.page_count(paper.file_ref) if scan else 0
    accepted_before = set(
        conn.execute(select(practice_item.c.position).where(practice_item.c.task_id == task.id))
        .scalars()
        .all()
    )
    env_error = _envelope_error(payload)
    if env_error is not None:
        judged = Judged(errors=[env_error])
    else:
        judged = _judge(
            conn, user_id, course_id, payload["items"], accepted_before, scan=scan, pages=pages
        )

    landed = set()
    for item in judged.accepted:
        topic_id = topics.topic_for(conn, user_id, course_id, item["topic"])
        _write_item(conn, task, task.scope_id, item, topic_id, item.get("page") if scan else None)
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
            # a typed paper counts at once; a scan, `partial` too, waits for his confirm (D-210)
            .values(transcript_task_id=task.id, transcript_confirmed_at=None if scan else now())
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
    if scan:
        reply.update(_read_back(conn, task.id, paper, pages, payload))
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


def _submit_tagging(conn: Connection, user_id: str, task, payload: Any) -> tuple[dict, dict]:
    """One submission for a tagging task, inside the caller's write unit (D-93, D-102). The
    envelope is checked once - `[]` is a valid answer - then each entry alone: schema -> an
    `active` topic of the material's course -> a second copy of an id in this payload is an
    error, the first is kept (D-105). A valid id whose pair (topic, this material) is already
    linked is `skipped`; otherwise its edge is written and it is `accepted`. Never a topic, never
    an edge removed. Returns the reply - topic ids in place of positions; `missing` is always empty,
    since the server has no list of topics it expects - and its log line's fields."""
    course_id = conn.execute(
        select(material.c.course_id).where(material.c.id == task.scope_id)
    ).scalar_one()

    accepted: list[str] = []
    skipped: list[str] = []
    errors: list[dict] = []
    env_error = _envelope_error(payload, _TAGGING_ENVELOPE_CHECK, "topics")
    if env_error is not None:
        errors.append(env_error)
    else:
        seen: set[str] = set()
        for place, entry in enumerate(payload["topics"], start=1):
            invalid = _schema_errors(place, entry, _TAGGING_ENTRY_CHECK, TAGGING_ENTRY_SCHEMA)
            if invalid:
                errors.extend(invalid)
            elif not topics.is_live_topic(conn, user_id, course_id, entry["id"], ("active",)):
                errors.append(
                    _item_error(
                        place,
                        "unknown_topic",
                        "id",
                        f"{entry['id']} is not an active topic of this course - use an id from "
                        "rules.topics",
                    )
                )
            elif entry["id"] in seen:
                errors.append(
                    _item_error(
                        place,
                        "duplicate_topic",
                        "id",
                        f"{entry['id']} is already in this payload - the first copy was kept",
                    )
                )
            else:
                seen.add(entry["id"])
                if topics.material_edge_if_missing(conn, user_id, entry["id"], task.scope_id):
                    accepted.append(entry["id"])
                else:
                    skipped.append(entry["id"])

    ordinal = _next_ordinal(conn, task.id)
    if not errors:
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
        "accepted": accepted,
        "skipped": skipped,
        "missing": [],
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


def _submit_practice(conn: Connection, user_id: str, task, payload: Any) -> tuple[dict, dict]:
    """One submission for a practice task, inside the caller's write unit (D-11, D-22, D-211). The
    envelope is checked once, then each item alone (`_judge_practice`); the first valid items up to
    `item_count` in all - earlier submissions count - are kept and the rest dropped. Each kept
    item is a `generated` practice_item: its key `unverified`, its novelty score stored and never
    a gate. Returns the reply - places in this payload in place of positions, `missing` always
    empty - and its log line's fields."""
    course_id = conn.execute(
        select(material.c.course_id).where(material.c.id == task.scope_id)
    ).scalar_one()

    env_error = _envelope_error(payload, _PRACTICE_ENVELOPE_CHECK)
    if env_error is not None:
        judged = Judged(errors=[env_error])
    else:
        judged = _judge_practice(conn, user_id, course_id, payload["items"])
    if task.item_count is None:
        kept = judged.accepted
    else:  # the count is the task's total: earlier submissions' items use it up (D-211)
        landed = conn.execute(
            select(func.count())
            .select_from(practice_item)
            .where(practice_item.c.task_id == task.id)
        ).scalar_one()
        kept = judged.accepted[: max(task.item_count - landed, 0)]

    for item in kept:
        _write_generated(conn, user_id, task, item)

    ordinal = _next_ordinal(conn, task.id)
    if not judged.errors:
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
        "accepted": [item["place"] for item in kept],
        "skipped": [],
        "missing": [],
        "item_errors": judged.errors,
        "submissions_left": MAX_SUBMISSIONS - ordinal if status == "open" else 0,
    }
    # an envelope error names no item, so it rejects none (D-52)
    rejected = len({e["item_ordinal"] for e in judged.errors} - {None})
    received = _record_submission(conn, user_id, task, ordinal, payload, reply, len(kept), rejected)
    event = {
        "submission": ordinal,
        "status": status,
        "accepted": len(kept),
        "rejected": rejected,
        "dropped": len(judged.accepted) - len(kept),
        "parked": sum(1 for item in kept if item["state"] == "needs-review"),
        "bytes": received,
    }
    return reply, event


def _judge_practice(conn: Connection, user_id: str, course_id: str, items: list) -> Judged:
    """Sort one submission's items into `accepted` and `errors`, each item judged alone.

    An accepted entry is the item's own dict plus two fields: "place" (its place in this
    payload, from 1) and "state" ("live", or "needs-review" when its topic is not `active` -
    D-22's gate). An error is `_item_error(place, code, field, message)`.
    """

    accepted = []
    errors = []
    for place, item in enumerate(items, start=1):
        unvalid = _schema_errors(place, item, _PRACTICE_ITEM_CHECK, PRACTICE_ITEM_SCHEMA)
        if unvalid:
            errors.extend(unvalid)
            continue
        is_usable = topics.is_live_topic(conn, user_id, course_id, item["topic_id"])
        if not is_usable:
            errors.append(
                _item_error(
                    place,
                    "unknown_topic",
                    "topic_id",
                    f"{item['topic_id']} is not a topic of this course - use an id from "
                    "rules.topics",
                )
            )
            continue
        is_live = topics.is_live_topic(conn, user_id, course_id, item["topic_id"], ("active",))
        state = "needs-review"
        if is_live:
            state = "live"
        accepted.append({**item, "place": place, "state": state})
    return Judged(accepted, errors)


def _shingles(text: str) -> set[str]:
    """A text as its pairs of neighbouring words, lowercased, punctuation dropped. A text of one
    word has no pair, so its set is that word - two identical one-word texts still match."""
    words = re.findall(r"\w+", text.lower())
    if len(words) < 2:
        return set(words)
    return {f"{a} {b}" for a, b in zip(words, words[1:], strict=False)}


def _overlap(a: set[str], b: set[str]) -> float:
    """Shared over total distinct: 1.0 for identical sets, 0.0 for none shared or two empty."""
    total = a | b
    return len(a & b) / len(total) if total else 0.0


def _novelty(
    conn: Connection, user_id: str, topic_id: str, question: str
) -> tuple[float, str | None]:
    """How close `question` is to what the pool already holds on its topic: (score, nearest) -
    score 0 to 1 (1.0 = it reproduces an existing item), nearest the practice_item id it scored
    closest to, or None when the topic has no item yet (D-24, D-211). Lexical: word-pair overlap,
    no model. The pool is the topic's past-exam and earlier generated items that are not
    rejected or superseded - an item written earlier in this same submission is in it too. The
    score is the highest overlap, so a copy of one item is not diluted by a big pool."""
    mine = _shingles(question)
    pool = conn.execute(
        select(practice_item.c.id, practice_item.c.question).where(
            practice_item.c.user_id == user_id,
            practice_item.c.topic_id == topic_id,
            practice_item.c.state.in_(("live", "needs-review")),
        )
    ).all()
    best, nearest = 0.0, None
    for row in pool:
        score = _overlap(mine, _shingles(row.question))
        if nearest is None or score > best:
            best, nearest = score, row.id
    return best, nearest


def _write_generated(conn: Connection, user_id: str, task, item: dict) -> None:
    """One kept item: its practice_item row. No evidence row - a generated item is no exam
    evidence (D-50 is for transcribed questions)."""
    score, nearest = _novelty(conn, user_id, item["topic_id"], item["question"])
    ordinal = _next_item_ordinal(conn, task.id)
    conn.execute(
        insert(practice_item).values(
            id=new_id(),
            user_id=user_id,
            topic_id=item["topic_id"],
            task_id=task.id,
            item_ordinal=ordinal,
            origin="generated",
            question=item["question"],
            answer_key=item["answer_key"],
            answer_provenance="unverified",  # the host's own key is never verified (D-16, D-211)
            state=item["state"],
            source_marker=item["source"],
            nearest_item_id=nearest,
            novelty_score=score,
            created_at=now(),
        )
    )


def _next_item_ordinal(conn: Connection, task_id: str) -> int:
    """The next free `item_ordinal` on a task. Items are numbered as they land, not by their
    place in one payload, so a retry that resends one fixed item does not collide."""
    top = conn.execute(
        select(func.max(practice_item.c.item_ordinal)).where(practice_item.c.task_id == task_id)
    ).scalar_one()
    return (top or 0) + 1


def _submit_observation(conn: Connection, user_id: str, task, payload: Any) -> tuple[dict, dict]:
    """One submission for an observation task, inside the caller's write unit (D-149, D-154). The
    envelope is checked once, then each claim alone - `topic-marks` refused (D-151), then the
    schema - then the survivors together against the claims already on the paper
    (`claims.judge_claims`). Each accepted claim is one `observation` (class fixed by its key, E1)
    and its `evidence` row. Returns the reply - places in this payload in place of positions,
    `missing` always empty - and its log line's fields."""
    slot_id = conn.execute(
        select(past_exam.c.slot_id).where(past_exam.c.id == task.scope_id)
    ).scalar_one()
    stored = [
        {"claim_key": r.claim_key, "claim_value": json.loads(r.claim_value)}
        for r in conn.execute(
            select(observation.c.claim_key, observation.c.claim_value).where(
                observation.c.past_exam_id == task.scope_id
            )
        )
    ]

    errors: list[dict] = []
    survivors: list[tuple[int, dict]] = []
    env_error = _envelope_error(payload, _OBSERVATION_ENVELOPE_CHECK, "claims")
    if env_error is not None:
        errors.append(env_error)
    else:
        for place, claim in enumerate(payload["claims"], start=1):
            if isinstance(claim, dict) and claim.get("claim_key") == "topic-marks":
                errors.append(
                    _item_error(
                        place,
                        "topic_marks_refused",
                        "claim_key",
                        "topic weights come from the transcribed questions (D-151) - leave "
                        "topic-marks out",
                    )
                )
                continue
            invalid = _schema_errors(place, claim, _CLAIM_CHECK, CLAIM_SCHEMA)
            if invalid:
                errors.extend(invalid)
            else:
                survivors.append((place, claim))
    judged = claims.judge_claims(survivors, stored)
    errors = sorted(errors + judged.errors, key=lambda e: e["item_ordinal"] or 0)

    by_place = dict(survivors)
    for place in judged.accepted:
        _write_claim(conn, user_id, slot_id, task.scope_id, by_place[place])

    ordinal = _next_ordinal(conn, task.id)
    if not errors:
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
        "accepted": sorted(judged.accepted),
        "skipped": sorted(judged.skipped),
        "missing": [],
        "item_errors": errors,
        "submissions_left": MAX_SUBMISSIONS - ordinal if status == "open" else 0,
    }
    # an envelope error names no claim, so it rejects none (D-52)
    rejected = len({e["item_ordinal"] for e in errors} - {None})
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


def _write_claim(conn: Connection, user_id: str, slot_id: str, paper_id: str, claim: dict) -> None:
    """One accepted claim: its `observation` row, E1 from the paper itself, and the `evidence`
    row pointing at it (D-149)."""
    observation_id = new_id()
    conn.execute(
        insert(observation).values(
            id=observation_id,
            owner_id=user_id,
            slot_id=slot_id,
            past_exam_id=paper_id,
            claim_key=claim["claim_key"],
            claim_class=claims.CLASS_OF[claim["claim_key"]],
            topic_id=None,
            claim_value=json.dumps(claim["claim_value"]),
            study_tier="E1",
            citation=claim.get("citation"),
            created_at=now(),
        )
    )
    conn.execute(
        insert(evidence).values(
            id=new_id(),
            user_id=user_id,
            observation_id=observation_id,
            study_tier="E1",
            locator=claim.get("citation"),
            created_at=now(),
        )
    )


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
    """The task a submission is for: this user's, and still open. An `invalidated` one that kept
    no note is told to start again (D-188); any other closed one has nothing to resend."""
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
        fix = "nothing to resend - its accepted items are in the pool"
        if task.status == "invalidated":  # its profile was derived again (D-11)
            # A kept note blocks a new start (`already_noted`), so only a task that kept
            # nothing is sent back to start (D-188).
            kept = conn.execute(select(note.c.id).where(note.c.task_id == task.id).limit(1)).first()
            if kept is None:
                fix = (
                    "start it again with study_start_generation - the exam profile it was built "
                    "on was derived again, so its weights may have changed (D-11)"
                )
            else:
                fix = "nothing to resend - the note it kept is saved, built on the older profile"
        raise StudyError(
            code="task_closed",
            message=f"this task is {task.status} and takes no more submissions",
            fix=fix,
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
    """The payload's one envelope error - it is not {key: [a list]}, non-empty for a
    transcription or a note (a tagging may send []) - or None."""
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
    """Every way one item breaks its schema - a transcription item, a note or a tagging entry -
    as item
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


def _read_back(conn: Connection, task_id: str, paper, pages: int, payload: Any) -> dict:
    """A scan's reply additions (D-210): `pages_empty`, the pages no item on the task names -
    a flag, never a refusal, since a cover may hold no question - and `read_back`, every item on
    the task beside its page plus page 1's header beside the registered row, for him to compare
    with the paper before he confirms it."""
    # read after this submission's writes: items accepted earlier are not in this payload
    rows = conn.execute(
        select(practice_item.c.position, practice_item.c.page, practice_item.c.question)
        .where(practice_item.c.task_id == task_id)
        .order_by(practice_item.c.position)
    ).all()
    used = {r.page for r in rows}
    # a paper may print no header: a missing one shows as null, never refused
    read = payload.get("header") if isinstance(payload, dict) else None
    return {
        "pages_empty": [p for p in range(1, pages + 1) if p not in used],
        "read_back": {
            "header": {
                "read": read,
                "registered": {
                    "date": paper.session_date,
                    "course": paper.code,
                    "instructor": paper.instructor,
                },
            },
            "items": [
                {"position": r.position, "page": r.page, "question": r.question} for r in rows
            ],
        },
    }


def _judge(
    conn: Connection,
    user_id: str,
    course_id: str,
    items: list,
    accepted_before: set[int],
    *,
    scan: bool,
    pages: int,
) -> Judged:
    """Sort one submission's items into accepted, errors and skipped. On a scan every item names
    its page, 1 to `pages` (D-210)."""
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
        # after the skip: a resent item already on the task is skipped, never an error
        elif scan and "page" not in item:
            errors.append(
                _item_error(
                    place,
                    "missing_page",
                    "page",
                    "this paper is a scan - give each question the page it is on, from 1",
                )
            )
        elif scan and item["page"] > pages:
            errors.append(
                _item_error(
                    place,
                    "unknown_page",
                    "page",
                    f"this paper has {pages} pages - page {item['page']} is not one of them",
                )
            )
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


def _write_item(
    conn: Connection, task, paper_id: str, item: dict, topic_id: str, page: int | None
) -> None:
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
            page=page,  # NULL on a typed paper, whatever the host sent (D-210)
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
