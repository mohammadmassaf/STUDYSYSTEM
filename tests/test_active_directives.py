"""`active_directives[]` (2.10; D-24, D-214): a directive he confirmed rides the next note or
practice payload for its course, word for word; a proposed one does not. Chapter 6 of I3302 is the
material throughout."""

# ruff: noqa: F811 - the fixtures are imported, then named as test parameters

import pytest
from sqlalchemy import insert, select

from studysystem.db.tables import coverage, generation_directive
from studysystem.services import generation
from studysystem.services.courses import add_course
from studysystem.services.feedback import confirm_directive, submit_feedback
from studysystem.services.generation import start_generation
from studysystem.services.ids import new_id, now
from studysystem.services.parts import Part
from studysystem.services.units import write_unit
from tests.test_generation import paper1  # noqa: F401
from tests.test_note_generation import ch6, i3302, web  # noqa: F401
from tests.test_practice_generation import set_topic_status
from tests.test_profiles import SEM

SHORT_MCQ = "Prefer short MCQ with four options over open questions."


def practice(engine, user_id, material_id):
    return start_generation(engine, user_id, "practice", material_id=material_id)


def directive(engine, user_id, text, course_id=None, topic_id=None, status="active") -> str:
    """A directive row written directly - shorter than the feedback that would draft it."""
    row_id = new_id()
    with write_unit(engine) as conn:
        conn.execute(
            insert(generation_directive).values(
                id=row_id,
                user_id=user_id,
                course_id=course_id,
                topic_id=topic_id,
                kind="generator-correction",
                text=text,
                status=status,
                superseded_by=row_id if status == "superseded" else None,
                created_at=now(),
                confirmed_at=None if status == "proposed" else now(),
            )
        )
    return row_id


def sent(task) -> list[dict]:
    return task.payload["rules"]["active_directives"]


# --- Done when: a confirmed directive rides, a proposed one does not --------------------


def test_a_confirmed_directive_rides_word_for_word_and_a_proposed_one_does_not(
    service_engine, user_id, ch6
):
    kept = submit_feedback(
        service_engine, user_id, "format-preference", directive_text=SHORT_MCQ, code="I3302"
    )["directive"]
    submit_feedback(
        service_engine,
        user_id,
        "format-preference",
        directive_text="Never ask about deprecated mysql_* functions.",
        code="I3302",
    )
    confirm_directive(service_engine, user_id, kept["id"])

    task = practice(service_engine, user_id, ch6)

    assert sent(task) == [{"text": SHORT_MCQ, "scope": "course", "applies_to": "I3302"}]
    assert task.payload["part"] == 1


# --- scope (D-214): the course, never the material's coverage ---------------------------


def test_a_topic_directive_rides_though_the_material_does_not_cover_its_topic(
    service_engine, user_id, web, ch6
):
    topics = practice(service_engine, user_id, ch6).payload["rules"]["topics"]
    target = topics[-1]
    with service_engine.connect() as conn:
        covered = conn.execute(
            select(coverage.c.id).where(
                coverage.c.material_id == ch6, coverage.c.topic_id == target["id"]
            )
        ).first()
    assert covered is None  # Chapter 6 is untagged: no coverage row for this topic

    directive(service_engine, user_id, "Give the SQL first.", web["course_id"], target["id"])

    assert sent(practice(service_engine, user_id, ch6)) == [
        {"text": "Give the SQL first.", "scope": "topic", "applies_to": target["name"]}
    ]


def test_an_all_courses_directive_rides_and_another_courses_does_not(service_engine, user_id, ch6):
    other = add_course(service_engine, user_id, "I3304", "Networks", SEM)
    directive(service_engine, user_id, "For I3304 only.", other["course_id"])
    directive(service_engine, user_id, "For every course.")

    assert sent(practice(service_engine, user_id, ch6)) == [
        {"text": "For every course.", "scope": "all", "applies_to": None}
    ]


def test_superseded_retired_topic_and_another_users_directives_stay_out(
    service_engine, user_id, other_user_id, web, ch6
):
    topics = practice(service_engine, user_id, ch6).payload["rules"]["topics"]
    retired = topics[0]["id"]
    directive(service_engine, user_id, "Superseded.", web["course_id"], status="superseded")
    directive(service_engine, user_id, "On a declined topic.", web["course_id"], retired)
    directive(service_engine, other_user_id, "Not his.", web["course_id"])
    set_topic_status(service_engine, retired, "declined")

    assert sent(practice(service_engine, user_id, ch6)) == []


def test_directives_travel_oldest_confirmation_first(service_engine, user_id, web, ch6):
    first = directive(service_engine, user_id, "First.", web["course_id"])
    second = directive(service_engine, user_id, "Second.")
    assert first < second  # ULIDs sort by time; the two may share one confirmed_at second

    assert [d["text"] for d in sent(practice(service_engine, user_id, ch6))] == [
        "First.",
        "Second.",
    ]


# --- kinds (D-214): practice and note carry them, tagging does not ----------------------


def test_a_note_task_carries_them_and_a_tagging_task_does_not(service_engine, user_id, web, ch6):
    directive(service_engine, user_id, SHORT_MCQ, web["course_id"])

    note = start_generation(service_engine, user_id, "note", material_id=ch6)
    tagging = start_generation(service_engine, user_id, "tagging", material_id=ch6)

    assert [d["text"] for d in sent(note)] == [SHORT_MCQ]
    assert "active_directives" not in tagging.payload["rules"]


@pytest.mark.parametrize("kind", ["transcription", "observation"])
def test_a_paper_task_carries_none(service_engine, user_id, web, paper1, kind):
    """A transcription copies the paper and an observation reads it: a directive could only
    distort what the profile is built from (D-214)."""
    directive(service_engine, user_id, SHORT_MCQ, web["course_id"])

    task = start_generation(service_engine, user_id, kind, past_exam_id=paper1)

    assert "active_directives" not in task.payload["rules"]


def test_no_directive_sends_an_empty_list(service_engine, user_id, ch6):
    """The field is always there on a practice task, so the host can tell 'none' from 'missing'."""
    assert sent(practice(service_engine, user_id, ch6)) == []


# --- parts (D-145): the list rides part 1 only -------------------------------------------


@pytest.mark.parametrize("kind", ["practice", "note"])
def test_the_list_rides_part_1_and_a_later_part_carries_none(
    service_engine, user_id, web, ch6, monkeypatch, kind
):
    """Each kind pages its own long list - a practice's topics, a note's past-exam items."""

    def one_page_a_part(head, pages, extras):
        parts = [Part(pages=[i]) for i in range(len(pages))]
        parts[-1].extras = list(range(len(extras)))
        return parts

    monkeypatch.setattr(generation, "split", one_page_a_part)
    directive(service_engine, user_id, SHORT_MCQ, web["course_id"])

    first = start_generation(service_engine, user_id, kind, material_id=ch6, part=1)
    second = start_generation(service_engine, user_id, kind, material_id=ch6, part=2)

    assert (first.payload["parts"], [d["text"] for d in sent(first)]) == (3, [SHORT_MCQ])
    assert "active_directives" not in second.payload.get("rules", {})
