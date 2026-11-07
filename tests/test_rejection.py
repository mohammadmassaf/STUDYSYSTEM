"""Rejection with a wrong answer key (4.2; D-16, D-213, D-257) - on both engines.

Each test follows I3302's 2023-09-13 Q3 (35 marks, prepared statements): the paper prints no
answer, so the host grades with its own key. He is marked wrong on Nov 8, solves a 2020 question
on the same topic right on Nov 9, and on Nov 10 learns the host's key for Q3 was wrong."""

import pytest
from sqlalchemy import func, select, update

from studysystem.db.tables import attempt, item_rejection, practice_item
from studysystem.errors import StudyError
from studysystem.services import feedback
from studysystem.services.courses import add_course
from studysystem.services.feedback import submit_feedback
from studysystem.services.study_loop import start_session, submit_attempts
from studysystem.services.units import write_unit
from tests.test_study_loop_services import _clock, _items, _state, _topic

SEM = "Semester 1 2026-2027"

# Noon UTC is the same local day at every offset from -11 to +11, so each stamp is one day.
NOV_8 = "2026-11-08T12:00:00Z"
NOV_9 = "2026-11-09T12:00:00Z"


@pytest.fixture
def web(service_engine, user_id):
    return add_course(service_engine, user_id, "I3302", "Server-Side Web Development", SEM)


@pytest.fixture
def q3(service_engine, user_id, web, monkeypatch):
    """Prepared statements with Q3 (35 marks) and a 2020 question (20 marks). Returns a dict with
    the topic, both items, the session and the study loop's clock."""
    clock = _clock(monkeypatch)
    prepared = _topic(
        service_engine, user_id, web["course_id"], "PHP + MySQL (prepared statements)"
    )
    [q3_id] = _items(service_engine, user_id, prepared, [35])
    [q2020] = _items(service_engine, user_id, prepared, [20])
    session = start_session(service_engine, user_id, "I3302", "practice", "topic", prepared)
    return {
        "topic": prepared,
        "q3": q3_id,
        "q2020": q2020,
        "session": session["session_id"],
        "clock": clock,
    }


def _attempt(engine, uid, case, stamp, part):
    case["clock"]["now"] = stamp
    submit_attempts(engine, uid, "I3302", [part], case["session"])


def _q3_wrong_on_nov_8(engine, uid, case):
    _attempt(engine, uid, case, NOV_8, {"practice_item_id": case["q3"], "correct": False})


def _wrong_key(engine, uid, item_id, **extra):
    return submit_feedback(
        engine,
        uid,
        kind="item-defect",
        practice_item_id=item_id,
        reason="wrong-answer-key",
        **extra,
    )


def _voided(engine, item_id):
    with engine.connect() as conn:
        return [
            row.correctness_voided
            for row in conn.execute(
                select(attempt.c.correctness_voided).where(attempt.c.practice_item_id == item_id)
            )
        ]


def _rejections(engine):
    with engine.connect() as conn:
        return conn.execute(select(item_rejection)).all()


def _item_state(engine, item_id):
    with engine.connect() as conn:
        return conn.execute(
            select(practice_item.c.state).where(practice_item.c.id == item_id)
        ).scalar_one()


def test_voiding_his_only_attempt_deletes_the_topics_state(service_engine, user_id, q3):
    """Case 1: Q3 was his only attempt on prepared statements. Voided, nothing counts, so the
    state is deleted (D-107) - the topic reads as never reviewed. The attempt row stays."""
    _q3_wrong_on_nov_8(service_engine, user_id, q3)
    assert _state(service_engine, q3["topic"]) is not None

    result = _wrong_key(service_engine, user_id, q3["q3"])

    assert result["voided"] == 1
    assert _voided(service_engine, q3["q3"]) == [1]
    assert _state(service_engine, q3["topic"]) is None
    assert _item_state(service_engine, q3["q3"]) == "rejected"
    [row] = _rejections(service_engine)
    assert (row.id, row.reason) == (result["rejection_id"], "wrong-answer-key")


def test_the_replay_keeps_the_days_that_still_count(service_engine, user_id, q3):
    """Case 2: Nov 8 Again (Q3), Nov 9 Good (2020) -> reps 2, lapses 1. Voiding Q3 leaves only
    Nov 9: reps 1, lapses 0."""
    _q3_wrong_on_nov_8(service_engine, user_id, q3)
    _attempt(service_engine, user_id, q3, NOV_9, {"practice_item_id": q3["q2020"], "correct": True})
    before = _state(service_engine, q3["topic"])
    assert (before.reps, before.lapses) == (2, 1)

    _wrong_key(service_engine, user_id, q3["q3"])

    after = _state(service_engine, q3["topic"])
    assert (after.reps, after.lapses) == (1, 0)
    assert _voided(service_engine, q3["q2020"]) == [0]


def test_an_item_less_attempt_on_the_topic_still_counts(service_engine, user_id, q3):
    """Case 3: a question noticed in class, logged on the topic with no item, is not Q3's - it
    keeps counting, so the state survives the voiding."""
    _q3_wrong_on_nov_8(service_engine, user_id, q3)
    _attempt(service_engine, user_id, q3, NOV_9, {"topic_id": q3["topic"], "correct": True})

    _wrong_key(service_engine, user_id, q3["q3"])

    after = _state(service_engine, q3["topic"])
    assert (after.reps, after.lapses) == (1, 0)
    with service_engine.connect() as conn:
        item_less = conn.execute(
            select(attempt.c.correctness_voided).where(attempt.c.practice_item_id.is_(None))
        ).scalar_one()
    assert item_less == 0


def test_another_reason_voids_nothing(service_engine, user_id, q3):
    """Case 4: off-syllabus is forward-only (D-16 e) - a correct key graded him, so the grade
    stays true and the state is untouched."""
    _q3_wrong_on_nov_8(service_engine, user_id, q3)
    before = _state(service_engine, q3["topic"])

    result = submit_feedback(
        service_engine,
        user_id,
        kind="item-defect",
        practice_item_id=q3["q3"],
        reason="off-syllabus",
    )

    assert result["voided"] == 0
    assert _voided(service_engine, q3["q3"]) == [0]
    assert _state(service_engine, q3["topic"]) == before


def test_a_wrong_key_found_after_another_rejection_changes_that_rows_reason(
    service_engine, user_id, q3
):
    """Case 5 (D-257): Q3 filed as unclear on Nov 8, then the key turns out wrong. The same row
    takes the new reason, the old one is kept in the note, and the attempt is voided."""
    _q3_wrong_on_nov_8(service_engine, user_id, q3)
    first = submit_feedback(
        service_engine,
        user_id,
        kind="item-defect",
        practice_item_id=q3["q3"],
        reason="unclear",
        note="part b is ambiguous",
    )

    second = _wrong_key(service_engine, user_id, q3["q3"], note="the key binds the wrong type")

    [row] = _rejections(service_engine)
    assert row.id == first["rejection_id"] == second["rejection_id"]
    assert row.reason == "wrong-answer-key"
    assert row.note == "was: unclear (part b is ambiguous); the key binds the wrong type"
    assert second["voided"] == 1
    assert _state(service_engine, q3["topic"]) is None


@pytest.mark.parametrize(
    ("filed", "sent"),
    [("unclear", "off-syllabus"), ("wrong-answer-key", "wrong-answer-key")],
)
def test_a_filed_item_is_refused_unless_the_key_turns_out_wrong(
    service_engine, user_id, q3, filed, sent
):
    """Cases 6-7: a second non-key reason is refused as before, and a key already voided is not
    voided again. Nothing changes."""
    _q3_wrong_on_nov_8(service_engine, user_id, q3)
    submit_feedback(
        service_engine, user_id, kind="item-defect", practice_item_id=q3["q3"], reason=filed
    )
    state = _state(service_engine, q3["topic"])

    with pytest.raises(StudyError) as refused:
        submit_feedback(
            service_engine, user_id, kind="item-defect", practice_item_id=q3["q3"], reason=sent
        )

    assert refused.value.code == "not_found"
    [row] = _rejections(service_engine)
    assert row.reason == filed
    assert _state(service_engine, q3["topic"]) == state


def test_an_unfiled_rejected_item_gets_its_row_and_its_voiding(service_engine, user_id, q3):
    """Case 8: an item rejected by hand with no row (like D-136's four) takes a wrong key: a new
    row, and its attempt voided."""
    _q3_wrong_on_nov_8(service_engine, user_id, q3)
    with write_unit(service_engine) as conn:
        conn.execute(
            update(practice_item).where(practice_item.c.id == q3["q3"]).values(state="rejected")
        )

    result = _wrong_key(service_engine, user_id, q3["q3"])

    assert len(_rejections(service_engine)) == 1
    assert result["voided"] == 1
    assert _state(service_engine, q3["topic"]) is None


def test_a_wrong_key_on_an_unattempted_item_voids_nothing(service_engine, user_id, q3):
    """Case 9: Q3 never attempted - the row is written, nothing voided, the 2020 question's state
    left as it was."""
    _attempt(service_engine, user_id, q3, NOV_9, {"practice_item_id": q3["q2020"], "correct": True})
    before = _state(service_engine, q3["topic"])

    result = _wrong_key(service_engine, user_id, q3["q3"])

    assert result["voided"] == 0
    assert len(_rejections(service_engine)) == 1
    assert _state(service_engine, q3["topic"]) == before


def test_a_failed_replay_rolls_back_the_whole_rejection(service_engine, user_id, q3, monkeypatch):
    """Case 10: one write unit. The replay fails, so the row, Q3's state and the voiding all roll
    back - no flagged attempt beside a state that still shows the old grade."""
    _q3_wrong_on_nov_8(service_engine, user_id, q3)
    before = _state(service_engine, q3["topic"])

    def broken_replay(*args, **kwargs):
        raise RuntimeError("replay failed")

    monkeypatch.setattr(feedback, "store_state", broken_replay)

    with pytest.raises(RuntimeError):
        _wrong_key(service_engine, user_id, q3["q3"])

    with service_engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(item_rejection)).scalar_one() == 0
    assert _item_state(service_engine, q3["q3"]) == "live"
    assert _voided(service_engine, q3["q3"]) == [0]
    assert _state(service_engine, q3["topic"]) == before
