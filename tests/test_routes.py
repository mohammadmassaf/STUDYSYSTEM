"""Route interchangeability (D-18, D-59): two hosts transcribe I3302 paper 1 through the same two
tools, each from the same fresh start. The same answers, written differently, must land as the
same rows - the contract does not care which model answered.

Compared is what the host writes: position, marks, topic name, question, answer_key, and whether
the task closed. Never what the server adds: ids, item_ordinal, origin, state, provenance."""

import asyncio
import json

from mcp import Client
from mcp.types import Implementation
from sqlalchemy import insert, select

from studysystem.db.tables import generation_task, metadata, practice_item, topic
from studysystem.server import create_server
from studysystem.services.courses import add_course
from studysystem.services.ids import new_id, now
from studysystem.services.past_exams import add_past_exam
from studysystem.services.units import write_unit
from studysystem.services.users import ensure_user
from tests._papers import pdf_pages, write

SEM = "Semester 1 2026-2027"
REGEX = "Regular expressions"
MYSQL = "PHP + MySQL (prepared statements)"
FORMS = "PHP forms"
SESSIONS = "PHP sessions & associative arrays"

# What paper 1 prints - the one truth both hosts answer from. Stand-in wording, not the paper's.
# (position, marks, topic, question, answer_key)
PAPER1 = [
    (1, None, REGEX, "Write a regex that matches a lowercase word.", "^[a-z]+$"),
    (
        2,
        None,
        REGEX,
        "Write a regex that matches a Lebanese phone number of the form 03-123456 or 71-123456.",
        None,
    ),
    (3, None, REGEX, "Explain what preg_match returns when the pattern does not match.", None),
    (4, 5, MYSQL, "Connect to the database with PDO and handle a failed connection.", None),
    (5, 9, MYSQL, "Insert a new student with a prepared statement.", None),
    (6, 7, MYSQL, "List the students of a given course, safe from SQL injection.", None),
    (7, 7, FORMS, "Build the form that posts a student's name and course.", None),
    (8, 6, SESSIONS, "Store the logged-in student's courses in the session.", None),
    (9, 7, SESSIONS, "Print each course and its grade from an associative array.", None),
]


def host_a(task: dict) -> dict:
    """Tags each item by an id from rules.topics; keys in schema order; paper order."""
    ids = {t["name"]: t["id"] for t in task["rules"]["topics"]}
    items = [
        {"position": p, "question": q, "answer_key": k, "marks": m, "topic": {"id": ids[name]}}
        for p, m, name, q, k in PAPER1
    ]
    return {"items": items}


def host_b(task: dict) -> dict:
    """The same answers, written differently: topics by name in capitals with spaces around,
    keys in another order, marks as floats, answer_key left out when there is none, Q2 first."""
    items = []
    for p, m, name, q, k in PAPER1:
        item = {
            "topic": {"proposed_name": f"  {name.upper()} "},
            "marks": None if m is None else float(m),
            "question": q,
            "position": p,
        }
        if k is not None:
            item["answer_key"] = k
        items.append(item)
    items.insert(0, items.pop(1))
    return {"items": items}


def cuts_q2(task: dict) -> dict:
    """Host A, but Q2 comes back with only half its text - still schema-valid."""
    payload = host_a(task)
    q2 = payload["items"][1]
    q2["question"] = q2["question"][: len(q2["question"]) // 2]
    return payload


def transcribe(engine, tmp_path, host: str, answer) -> tuple[str, list[tuple]]:
    """A fresh database holding I3302, paper 1 and its four topics; `host` transcribes the paper
    through the tools with `answer` as its model. Returns the task's status and what the host
    wrote: one (position, marks, topic name, question, answer_key) per item, in paper order."""
    metadata.drop_all(engine)
    metadata.create_all(engine)
    user_id = ensure_user(engine)
    course_id = add_course(engine, user_id, "I3302", "Server-Side Web Development", SEM)[
        "course_id"
    ]
    with write_unit(engine) as conn:
        for name in [REGEX, MYSQL, FORMS, SESSIONS]:
            conn.execute(
                insert(topic).values(
                    id=new_id(),
                    user_id=user_id,
                    course_id=course_id,
                    name=name,
                    status="active",
                    proposed_by="profile",
                    status_changed_at=now(),
                    created_at=now(),
                )
            )
    path = write(
        tmp_path / host, "I3302_First.pdf", pdf_pages(["Problem I", "Problem II"], pictured={2})
    )
    paper = add_past_exam(
        engine, user_id, "I3302", "Final exam", "first", "2020-02-17", path, "Dr. Hamze"
    )["past_exam_id"]

    async def go():
        client_info = Implementation(name=host, version="0")
        async with Client(create_server(engine), client_info=client_info) as client:
            started = await client.call_tool(
                "study_start_generation", {"kind": "transcription", "past_exam_id": paper}
            )
            assert not started.is_error, started.content[0].text
            text, *pictures = started.content
            assert [b.type for b in pictures] == ["image"]  # Problem II's figure reaches the host
            task = json.loads(text.text)
            submitted = await client.call_tool(
                "study_submit_generation", {"task_id": task["task_id"], "payload": answer(task)}
            )
            assert not submitted.is_error, submitted.content[0].text

    asyncio.run(go())

    with engine.connect() as conn:
        status = conn.execute(select(generation_task.c.status)).scalar_one()
        wrote = conn.execute(
            select(
                practice_item.c.position,
                practice_item.c.marks,
                topic.c.name,
                practice_item.c.question,
                practice_item.c.answer_key,
            )
            .join_from(practice_item, topic)
            .order_by(practice_item.c.position)
        ).all()
    return status, [(p, m, name.casefold(), q, k) for p, m, name, q, k in wrote]


def test_two_hosts_writing_the_same_answers_leave_the_same_rows(service_engine, tmp_path):
    status_a, a = transcribe(service_engine, tmp_path, "host-a", host_a)
    status_b, b = transcribe(service_engine, tmp_path, "host-b", host_b)

    assert status_a == status_b == "closed"
    assert len(a) == len(PAPER1)
    assert a == b


def test_a_host_that_cuts_a_question_is_told_apart(service_engine, tmp_path):
    """The compare is not blind: a cut question is schema-valid, so the task closes all the same -
    only the question column shows the damage."""
    _, a = transcribe(service_engine, tmp_path, "host-a", host_a)
    status_c, c = transcribe(service_engine, tmp_path, "cuts-q2", cuts_q2)

    assert status_c == "closed"
    differ = [mine[0] for mine, theirs in zip(a, c, strict=True) if mine != theirs]
    assert differ == [2]
