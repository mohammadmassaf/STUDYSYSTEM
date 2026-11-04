"""The Cowork plugin's study flows (D-236): one skill per slash command, the shared steps kept in
step across the skill files, and D-208's order rules."""

import asyncio
import json
import re
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from studysystem.server import create_server

PLUGIN = Path(__file__).parent.parent / "plugin"
SKILLS = ("study-plan", "study-day", "add-chapter", "explain-chapter")
SHARED = re.compile(r"<!-- shared:([a-z-]+) -->\n(.*?)<!-- /shared:\1 -->", re.DOTALL)


def body(skill: str) -> str:
    return (PLUGIN / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")


def test_manifest_names_the_plugin() -> None:
    manifest = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))

    assert manifest["name"] == "studysystem"


@pytest.mark.parametrize("skill", SKILLS)
def test_each_skill_is_named_after_its_folder(skill) -> None:
    front = body(skill).split("---")[1]

    assert f"name: {skill}\n" in front
    assert f"/{skill}" in front  # the description names its own slash command


def test_shared_steps_match_in_every_skill() -> None:
    copies: dict[str, set[str]] = {}
    for skill in SKILLS:
        for name, text in SHARED.findall(body(skill)):
            copies.setdefault(name, set()).add(text)

    assert set(copies) == {"plan-step", "make-note-step", "tag-step", "log-step"}
    # One wording per step: an edit to one copy that misses another fails here.
    assert all(len(texts) == 1 for texts in copies.values()), copies


@pytest.mark.parametrize("skill", ["study-day", "add-chapter", "explain-chapter"])
def test_each_working_skill_logs_its_minutes(skill) -> None:
    # The match test above passes for a skill that drops a block; this one does not.
    assert "<!-- shared:log-step -->" in body(skill)


def test_study_plan_makes_a_plan() -> None:
    assert "study_get_plan" in body("study-plan")


@pytest.mark.parametrize("skill", ["study-day", "explain-chapter"])
def test_skill_gets_the_plan_before_the_session_and_logs_on_its_item(skill) -> None:
    text = body(skill)

    # The gate counts an hour only against the plan in force when its session started (D-113).
    assert text.index("study_get_plan") < text.index("study_start_session")
    assert "topic_id or setup_key" in text
    assert "Always pass the session_id" in text


def test_add_chapter_notes_and_tags_and_teaches_nothing() -> None:
    text = body("add-chapter")

    assert text.index("study_get_plan") < text.index("study_add_material")
    # The note makes the chapter taught; only the tagging lets the plan lift its topics.
    assert text.index("kind `note`") < text.index("kind `tagging`")
    assert "study_start_session" not in text


def test_explain_chapter_makes_a_missing_note_before_the_session() -> None:
    text = body("explain-chapter")

    assert text.index("study_add_material") < text.index("study_start_generation")
    assert text.index("study_start_generation") < text.index("study_start_session")
    assert "already_noted" in text
    # Only /add-chapter links topics (D-236): explain points the student there instead.
    assert "kind `tagging`" not in text
    assert "/add-chapter" in text


def test_study_day_sends_a_first_material_item_to_add_chapter() -> None:
    # The command it names must exist, or the student looks for one that is gone.
    assert "/add-chapter" in body("study-day")
    assert (PLUGIN / "skills" / "add-chapter" / "SKILL.md").exists()


def test_server_offers_no_mcp_prompts() -> None:
    # The flows live in the plugin only (D-236), so there is one copy of each.
    server = create_server(create_engine("sqlite://"))

    assert asyncio.run(server.list_prompts()) == []
