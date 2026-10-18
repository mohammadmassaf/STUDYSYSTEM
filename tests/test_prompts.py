import asyncio

import pytest
from sqlalchemy import create_engine

from studysystem.server import create_server


def server():
    return create_server(create_engine("sqlite://"))  # a prompt never touches it


def text(name: str, args: dict) -> str:
    return asyncio.run(server().get_prompt(name, args)).messages[0].content.text


def test_prompts_are_listed_with_their_inputs() -> None:
    prompts = {p.name: p for p in asyncio.run(server().list_prompts())}

    assert not prompts["study_plan"].arguments
    assert not prompts["study_day"].arguments
    args = {a.name: a for a in prompts["study_chapter"].arguments}
    assert set(args) == {"code", "path"}
    assert all(a.required and a.description for a in args.values())


def test_study_plan_prompt_makes_a_plan() -> None:
    assert "study_get_plan" in text("study_plan", {})


def test_study_chapter_prompt_fills_in_course_and_path() -> None:
    body = text("study_chapter", {"code": "I3302", "path": "C:/notes/ch3.pdf"})

    assert "Course I3302" in body
    assert "C:/notes/ch3.pdf" in body


@pytest.mark.parametrize(
    ("name", "args"),
    [("study_day", {}), ("study_chapter", {"code": "I3302", "path": "C:/notes/ch3.pdf"})],
)
def test_prompt_gets_the_plan_before_the_session_and_logs_on_its_item(name, args) -> None:
    body = text(name, args)

    # The gate counts an hour only against the plan in force when its session started (D-113).
    assert body.index("study_get_plan") < body.index("study_start_session")
    assert "topic_id or setup_key" in body
