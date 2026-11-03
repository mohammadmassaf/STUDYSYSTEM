import asyncio

import pytest
from sqlalchemy import create_engine

from studysystem.server import create_server

CH3 = {"code": "I3302", "path": "C:/notes/ch3.pdf"}


def server():
    return create_server(create_engine("sqlite://"))  # a prompt never touches it


def text(name: str, args: dict) -> str:
    return asyncio.run(server().get_prompt(name, args)).messages[0].content.text


def test_prompts_are_listed_with_their_inputs() -> None:
    prompts = {p.name: p for p in asyncio.run(server().list_prompts())}

    assert not prompts["study_plan"].arguments
    assert not prompts["study_day"].arguments
    assert "study_chapter" not in prompts  # split in two (D-234)

    add = {a.name: a for a in prompts["study_add_chapter"].arguments}
    assert set(add) == {"code", "path"}
    assert all(a.required and a.description for a in add.values())

    explain = {a.name: a for a in prompts["study_explain_chapter"].arguments}
    assert set(explain) == {"code", "path", "note"}
    assert explain["code"].required and explain["path"].required
    assert not explain["note"].required
    assert all(a.description for a in explain.values())


def test_study_plan_prompt_makes_a_plan() -> None:
    assert "study_get_plan" in text("study_plan", {})


@pytest.mark.parametrize("name", ["study_add_chapter", "study_explain_chapter"])
def test_chapter_prompt_fills_in_course_and_path(name) -> None:
    body = text(name, CH3)

    assert "Course I3302" in body
    assert "C:/notes/ch3.pdf" in body


def test_add_chapter_makes_the_note_and_teaches_nothing() -> None:
    body = text("study_add_chapter", CH3)

    assert body.index("study_get_plan") < body.index("study_add_material")
    assert "study_start_generation kind 'note'" in body
    assert "study_start_session" not in body
    assert "topic_id or setup_key" in body


def test_explain_chapter_reads_the_note_only_when_given() -> None:
    assert "exam note is at" not in text("study_explain_chapter", CH3)

    body = text("study_explain_chapter", {**CH3, "note": "C:/vault/notes/ch3_notes.md"})
    assert "Its exam note is at C:/vault/notes/ch3_notes.md" in body


def test_explain_chapter_makes_a_missing_note_before_the_session() -> None:
    body = text("study_explain_chapter", CH3)

    # The guard (D-234): a chapter with no note, new or added earlier, gets one before teaching.
    assert body.index("study_add_material") < body.index("study_start_generation")
    assert body.index("study_start_generation") < body.index("study_start_session")
    assert "already_noted" in body


def test_study_day_sends_a_first_material_item_to_add_a_chapter() -> None:
    # The menu title it names must exist, or the student looks for a prompt that is gone.
    prompts = {p.name: p for p in asyncio.run(server().list_prompts())}

    assert prompts["study_add_chapter"].title == "Add a chapter"
    assert "'Add a chapter'" in text("study_day", {})


@pytest.mark.parametrize(("name", "args"), [("study_day", {}), ("study_explain_chapter", CH3)])
def test_prompt_gets_the_plan_before_the_session_and_logs_on_its_item(name, args) -> None:
    body = text(name, args)

    # The gate counts an hour only against the plan in force when its session started (D-113).
    assert body.index("study_get_plan") < body.index("study_start_session")
    assert "topic_id or setup_key" in body
    # Seen 2026-11-03: "pass neither" read as "no session_id either" left the session open.
    assert "Always pass the session_id" in body
