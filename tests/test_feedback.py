"""Feedback capture and directives (2.9; D-24, D-213): a rejection note on a bad item, the
directive it may draft, and the confirmation that makes the directive durable - on both engines.
Each test follows one of his I3302 Final items: Q7, a generated question he files against."""

import json

import pytest
from sqlalchemy import func, insert, select, update
from sqlalchemy.exc import IntegrityError

from studysystem.db.tables import (
    generation_directive,
    generation_task,
    item_rejection,
    observation,
    practice_item,
    topic,
)
from studysystem.errors import StudyError
from studysystem.services import feedback
from studysystem.services.courses import add_course
from studysystem.services.feedback import confirm_directive, submit_feedback
from studysystem.services.ids import new_id, now
from studysystem.services.units import write_unit

SEM = "Semester 1 2026-2027"


@pytest.fixture
def web(service_engine, user_id):
    return add_course(service_engine, user_id, "I3302", "Server-Side Web Development", SEM)


@pytest.fixture
def q7(service_engine, user_id, web):
    """Q7: one generated practice item on the topic Triggers. Returns (item id, topic id)."""
    topic_id, task_id, item_id = new_id(), new_id(), new_id()
    with write_unit(service_engine) as conn:
        conn.execute(
            insert(topic).values(
                id=topic_id,
                user_id=user_id,
                course_id=web["course_id"],
                name="Triggers",
                status="active",
                proposed_by="profile",
                status_changed_at=now(),
                created_at=now(),
            )
        )
        conn.execute(
            insert(generation_task).values(
                id=task_id,
                user_id=user_id,
                kind="practice",
                scope_type="topic",
                scope_id=topic_id,
                route="host",
                created_at=now(),
            )
        )
        conn.execute(
            insert(practice_item).values(
                id=item_id,
                user_id=user_id,
                topic_id=topic_id,
                task_id=task_id,
                item_ordinal=7,
                question="Write a BEFORE INSERT trigger that ...",
                answer_provenance="none",
                marks=4,
                novelty_score=0.5,
                origin="generated",
                source_marker="model-knowledge",
                created_at=now(),
            )
        )
    return item_id, topic_id


def _count(engine, table):
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def _item_state(engine, item_id):
    with engine.connect() as conn:
        return conn.execute(
            select(practice_item.c.state).where(practice_item.c.id == item_id)
        ).scalar_one()


def _directive(engine, directive_id):
    with engine.connect() as conn:
        return conn.execute(
            select(generation_directive).where(generation_directive.c.id == directive_id)
        ).one()


def test_rejection_note_drafts_a_proposed_directive(service_engine, user_id, web, q7):
    """Case 1: one item-defect call writes the rejection, retires Q7 and drafts the directive,
    scoped to Q7's topic by default."""
    item_id, topic_id = q7
    result = submit_feedback(
        service_engine,
        user_id,
        kind="item-defect",
        practice_item_id=item_id,
        reason="off-syllabus",
        note="triggers were not taught this year",
        directive_text="Do not generate questions on SQL triggers.",
    )

    with service_engine.connect() as conn:
        rejection = conn.execute(select(item_rejection)).one()
    assert rejection.id == result["rejection_id"]
    assert (rejection.practice_item_id, rejection.reason) == (item_id, "off-syllabus")
    assert rejection.note == "triggers were not taught this year"
    assert _item_state(service_engine, item_id) == "rejected"

    row = _directive(service_engine, result["directive"]["id"])
    assert row.status == "proposed"
    assert row.confirmed_at is None
    assert row.kind == "generator-correction"
    assert row.text == "Do not generate questions on SQL triggers."
    assert row.source_rejection_id == rejection.id
    assert (row.course_id, row.topic_id) == (web["course_id"], topic_id)


def test_confirming_flips_status_and_stamps_confirmed_at(service_engine, user_id, q7):
    """Case 2: the explicit yes - `proposed` becomes `active`, and only then has a timestamp."""
    item_id, _ = q7
    drafted = submit_feedback(
        service_engine,
        user_id,
        kind="item-defect",
        practice_item_id=item_id,
        reason="off-syllabus",
        directive_text="Do not generate questions on SQL triggers.",
    )
    directive_id = drafted["directive"]["id"]

    confirm_directive(service_engine, user_id, directive_id)

    row = _directive(service_engine, directive_id)
    assert row.status == "active"
    assert row.confirmed_at is not None


def test_course_scope_drops_the_topic(service_engine, user_id, web, q7):
    """Case C: the same note widened to the course - the directive keeps I3302, not Triggers."""
    item_id, _ = q7
    result = submit_feedback(
        service_engine,
        user_id,
        kind="item-defect",
        practice_item_id=item_id,
        reason="off-syllabus",
        directive_text="Do not generate questions on SQL triggers.",
        scope="course",
    )

    row = _directive(service_engine, result["directive"]["id"])
    assert (row.course_id, row.topic_id) == (web["course_id"], None)


def test_format_preference_writes_only_a_directive(service_engine, user_id, web):
    """Case D: no item - no rejection, and the directive is course-wide with no source."""
    result = submit_feedback(
        service_engine,
        user_id,
        kind="format-preference",
        directive_text="Show working in every answer.",
        code="I3302",
    )

    assert result["rejection_id"] is None
    assert _count(service_engine, item_rejection) == 0
    row = _directive(service_engine, result["directive"]["id"])
    assert (row.kind, row.status) == ("format-preference", "proposed")
    assert (row.course_id, row.topic_id, row.source_rejection_id) == (web["course_id"], None, None)


def test_generator_correction_about_difficulty_writes_no_observation(service_engine, user_id, q7):
    """Case 3: "too easy" is about the model's output, not the instructor - it must not reach
    the profile the mock gate reads (D-24)."""
    item_id, _ = q7
    submit_feedback(
        service_engine,
        user_id,
        kind="item-defect",
        practice_item_id=item_id,
        reason="wrong-difficulty",
        note="far easier than the real papers",
        directive_text="Make trigger questions as hard as the 2024 Final's.",
    )

    assert _count(service_engine, observation) == 0
    assert _count(service_engine, generation_directive) == 1


def test_wrong_answer_key_is_refused_until_4_2(service_engine, user_id, q7):
    """Case 4: voiding the attempts is 4.2's job, so 2.9 refuses the reason and writes nothing
    (D-213) - Q7 stays live."""
    item_id, _ = q7
    with pytest.raises(StudyError) as refused:
        submit_feedback(
            service_engine,
            user_id,
            kind="item-defect",
            practice_item_id=item_id,
            reason="wrong-answer-key",
        )

    assert refused.value.code == "invalid_value"
    assert refused.value.field_errors[0]["field"] == "reason"
    assert _count(service_engine, item_rejection) == 0
    assert _item_state(service_engine, item_id) == "live"


def test_feedback_through_the_tools(call, q7):
    """Both tools are wired: a rejection drafts a directive, the confirm makes it active, and a
    refusal comes back structured."""
    item_id, _ = q7
    drafted = call(
        "study_submit_feedback",
        {
            "kind": "item-defect",
            "practice_item_id": item_id,
            "reason": "off-syllabus",
            "directive_text": "Do not generate questions on SQL triggers.",
        },
    )
    assert not drafted.is_error
    directive_id = json.loads(drafted.content[0].text)["directive"]["id"]

    confirmed = call("study_confirm_directive", {"directive_id": directive_id})
    assert not confirmed.is_error
    assert json.loads(confirmed.content[0].text)["status"] == "active"

    again = call("study_confirm_directive", {"directive_id": directive_id})
    assert again.is_error
    assert json.loads(again.content[0].text)["code"] == "not_found"


def test_a_failed_directive_rolls_back_the_rejection(service_engine, user_id, q7, monkeypatch):
    """Case 5: one write unit. The directive insert fails (an id the ULID check refuses), so the
    rejection row and Q7's state roll back with it."""
    item_id, _ = q7
    real_new_id = feedback.new_id
    calls = []

    def first_id_good_then_bad():
        calls.append(1)
        return real_new_id() if len(calls) == 1 else "not-a-ulid"

    monkeypatch.setattr(feedback, "new_id", first_id_good_then_bad)

    with pytest.raises(IntegrityError):
        submit_feedback(
            service_engine,
            user_id,
            kind="item-defect",
            practice_item_id=item_id,
            reason="off-syllabus",
            directive_text="Do not generate questions on SQL triggers.",
        )

    assert _count(service_engine, item_rejection) == 0
    assert _count(service_engine, generation_directive) == 0
    assert _item_state(service_engine, item_id) == "live"


def _set_state(engine, item_id, state):
    with write_unit(engine) as conn:
        conn.execute(update(practice_item).where(practice_item.c.id == item_id).values(state=state))


def _reject(engine, uid, item_id, **extra):
    return submit_feedback(
        engine, uid, kind="item-defect", practice_item_id=item_id, reason="off-syllabus", **extra
    )


@pytest.mark.parametrize("state", ["needs-review", "rejected"])
def test_a_parked_or_unfiled_item_can_be_rejected(service_engine, user_id, q7, state):
    """D-213, his pick C: a `needs-review` item, and a `rejected` one with no rejection row yet
    (D-136's four, retired by the one-off UPDATE), both get their row."""
    item_id, _ = q7
    _set_state(service_engine, item_id, state)

    result = _reject(service_engine, user_id, item_id)

    assert result["rejection_id"] is not None
    assert _count(service_engine, item_rejection) == 1
    assert _item_state(service_engine, item_id) == "rejected"


def test_an_item_already_filed_is_not_rejected_twice(service_engine, user_id, q7):
    item_id, _ = q7
    _reject(service_engine, user_id, item_id)

    with pytest.raises(StudyError) as refused:
        _reject(service_engine, user_id, item_id)

    assert refused.value.code == "not_found"
    assert _count(service_engine, item_rejection) == 1


def test_a_blank_directive_text_is_refused(service_engine, user_id, q7):
    """QA's Gap 1: "   " would have stored a directive with empty text - an empty payload line
    in 2.10. Refused before anything is written."""
    item_id, _ = q7
    with pytest.raises(StudyError) as refused:
        _reject(service_engine, user_id, item_id, directive_text="   ")

    assert refused.value.field_errors[0]["field"] == "directive_text"
    assert _count(service_engine, item_rejection) == 0
    assert _item_state(service_engine, item_id) == "live"


def test_another_users_item_and_directive_are_not_found(service_engine, user_id, other_user_id, q7):
    """Ownership: the other user cannot reject his Q7 or confirm his directive."""
    item_id, _ = q7
    with pytest.raises(StudyError) as item_refused:
        _reject(service_engine, other_user_id, item_id)
    assert item_refused.value.code == "not_found"
    assert _item_state(service_engine, item_id) == "live"

    drafted = _reject(service_engine, user_id, item_id, directive_text="No triggers.")
    with pytest.raises(StudyError) as directive_refused:
        confirm_directive(service_engine, other_user_id, drafted["directive"]["id"])
    assert directive_refused.value.code == "not_found"
    assert _directive(service_engine, drafted["directive"]["id"]).status == "proposed"


def test_a_generator_correction_for_all_courses(service_engine, user_id):
    """kind generator-correction called directly, widened to every course: both scope columns
    NULL, and no observation (D-24)."""
    result = submit_feedback(
        service_engine,
        user_id,
        kind="generator-correction",
        directive_text="Never repeat a past-exam question word for word.",
        scope="all",  # no code: an all-courses directive stores none (D-213)
    )

    row = _directive(service_engine, result["directive"]["id"])
    assert (row.kind, row.course_id, row.topic_id) == ("generator-correction", None, None)
    assert _count(service_engine, observation) == 0


@pytest.mark.parametrize(
    ("extra", "field"),
    [
        ({"practice_item_id": "x" * 26}, "practice_item_id"),  # an item on an item-less kind
        ({"scope": "topic"}, "scope"),  # no item, so no topic
        ({"directive_text": None}, "directive_text"),  # the directive is the whole call
        ({"code": None}, "code"),  # one course, so the course is needed
        ({"directive_kind": "generator-correction"}, "directive_kind"),  # kind already says it
    ],
)
def test_an_item_less_kind_refuses_what_it_cannot_use(service_engine, user_id, web, extra, field):
    args = {"directive_text": "Show working in every answer.", "code": "I3302", **extra}
    with pytest.raises(StudyError) as refused:
        submit_feedback(service_engine, user_id, kind="format-preference", **args)

    assert refused.value.field_errors[0]["field"] == field
    assert _count(service_engine, generation_directive) == 0


def test_a_rejection_can_draft_a_format_preference(service_engine, user_id, q7):
    """D-213: "I prefer short MCQ" on a rejected item is about him, not the model - the call
    says so with directive_kind."""
    item_id, _ = q7
    result = _reject(
        service_engine,
        user_id,
        item_id,
        directive_text="I prefer short MCQ questions.",
        directive_kind="format-preference",
    )

    assert _directive(service_engine, result["directive"]["id"]).kind == "format-preference"


def test_a_blank_note_is_stored_as_null(service_engine, user_id, q7):
    """Unknown stays unknown: a note of spaces is no note."""
    item_id, _ = q7
    _reject(service_engine, user_id, item_id, note="   ")

    with service_engine.connect() as conn:
        assert conn.execute(select(item_rejection.c.note)).scalar_one() is None
