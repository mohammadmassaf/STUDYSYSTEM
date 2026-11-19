"""Discrimination trials in walkthroughs (4.13; D-24, D-312 - D-318), on both engines.

Trials are off by default (D-312). With them on, `study_start_session` hides one generated item
among a past paper's questions (D-314, D-316, D-318); each answer carries the guess `flagged`
and maybe a `tell`, and `study_submit_attempt` writes the attempt, then the trial, then reveals
where the item came from (D-315). The paper is a four-part I3302 MySQL problem; the generated
item is one more MySQL question."""

# ruff: noqa: F811 - the fixtures are imported, then named as test parameters

import json

import pytest
from sqlalchemy import func, select, update
from starlette.testclient import TestClient

from studysystem.dashboard import create_app
from studysystem.db.tables import attempt, discrimination_trial, practice_item, user
from studysystem.errors import StudyError
from studysystem.services import trials
from studysystem.services.courses import add_course
from studysystem.services.ids import new_id
from studysystem.services.study_loop import start_session, submit_attempts
from studysystem.services.units import write_unit
from tests.test_study_loop_services import _items, _paper, _topic

SEM = "Semester 1 2026-2027"
OWN = "http://127.0.0.1:8765"
QUESTION_KEYS = {"practice_item_id", "number", "question"}  # D-316, amended - see test 5


@pytest.fixture
def web(service_engine, user_id):
    return add_course(service_engine, user_id, "I3302", "Server-Side Web Development", SEM)


@pytest.fixture
def walk(service_engine, user_id, web):
    """The 2018-07-21 paper: four MySQL questions, plus one generated MySQL item never attempted
    - the one candidate. Returns {"topic", "paper", "real" (ids in paper order), "generated"}."""
    mysql = _topic(service_engine, user_id, web["course_id"], "MySQL")
    paper = _paper(service_engine, user_id, web["slot_id"])
    real = _items(service_engine, user_id, mysql, [5, 5, 15, 25], paper=paper)
    [generated] = _items(service_engine, user_id, mysql, [None])
    return {"topic": mysql, "paper": paper, "real": real, "generated": generated}


def _start(engine, uid, walk, mode="exam-walkthrough"):
    if mode == "exam-walkthrough":
        return start_session(engine, uid, "I3302", mode, "past-exam", walk["paper"])
    return start_session(engine, uid, "I3302", mode, "topic", walk["topic"])


def _trials(engine):
    with engine.connect() as conn:
        return conn.execute(
            select(discrimination_trial).order_by(discrimination_trial.c.created_at)
        ).all()


def _count(engine, table):
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


# --- hiding the item: study_start_session (D-314, D-316, D-318) -----------------------------


def test_1_trials_off_lists_the_papers_questions_in_order(service_engine, user_id, walk):
    session = _start(service_engine, user_id, walk)

    questions = session["questions"]
    assert [q["practice_item_id"] for q in questions] == walk["real"]
    assert [q["number"] for q in questions] == [1, 2, 3, 4]


def test_2_trials_on_one_generated_item_joins_the_list_never_first(service_engine, user_id, walk):
    trials.set_trials(service_engine, user_id, True)

    questions = _start(service_engine, user_id, walk)["questions"]

    ids = [q["practice_item_id"] for q in questions]
    assert len(ids) == 5
    assert ids.count(walk["generated"]) == 1
    assert ids[0] != walk["generated"]
    assert [i for i in ids if i != walk["generated"]] == walk["real"]  # the paper keeps its order
    assert [q["number"] for q in questions] == [1, 2, 3, 4, 5]


def test_3_the_same_seed_draws_the_same_item_and_place():
    seed = "01JD0000000000000000000000"
    candidates = ["a", "b", "c"]

    assert trials.draw(candidates, 4, seed) == trials.draw(candidates, 4, seed)
    places = {trials.draw(candidates, 4, f"{n:026d}")[1] for n in range(300)}
    assert places == {2, 3, 4, 5}  # every place but the first, up to the last
    assert trials.draw([], 4, seed) is None
    assert trials.draw(candidates, 0, seed) is None  # a paper with no questions hides nothing


@pytest.mark.parametrize("why", ["attempted", "rejected", "other-topic"])
def test_4_no_candidate_no_hidden_item(service_engine, user_id, web, walk, why):
    if why == "attempted":  # seen once, revealed once: no longer blind
        practice = _start(service_engine, user_id, walk, mode="practice")
        submit_attempts(
            service_engine,
            user_id,
            "I3302",
            [{"practice_item_id": walk["generated"], "correct": True}],
            practice["session_id"],
        )
    elif why == "rejected":
        with write_unit(service_engine) as conn:
            conn.execute(
                update(practice_item)
                .where(practice_item.c.id == walk["generated"])
                .values(state="rejected")
            )
    else:
        php = _topic(service_engine, user_id, web["course_id"], "PHP")
        with write_unit(service_engine) as conn:
            conn.execute(
                update(practice_item)
                .where(practice_item.c.id == walk["generated"])
                .values(topic_id=php)
            )
    trials.set_trials(service_engine, user_id, True)

    questions = _start(service_engine, user_id, walk)["questions"]

    assert [q["practice_item_id"] for q in questions] == walk["real"]


def test_5_every_question_has_the_same_shape(service_engine, user_id, walk):
    # marks and page are left out too: every generated item has neither, so either one would
    # tell the hidden item apart (live 2026-11-19: 5 of 5 generated items have no marks)
    trials.set_trials(service_engine, user_id, True)

    questions = _start(service_engine, user_id, walk)["questions"]

    assert all(set(q) == QUESTION_KEYS for q in questions)


@pytest.mark.parametrize("mode", ["practice", "explain-chapter"])
def test_6_another_mode_gets_no_question_list(service_engine, user_id, walk, mode):
    trials.set_trials(service_engine, user_id, True)

    session = _start(service_engine, user_id, walk, mode=mode)

    assert "questions" not in session


# --- recording the trial: study_submit_attempt (D-315, D-316) -------------------------------


def _answer(engine, uid, session_id, item, **guess):
    return submit_attempts(
        engine, uid, "I3302", [{"practice_item_id": item, "correct": True, **guess}], session_id
    )


def test_7_a_real_item_writes_a_trial_and_reveals_its_paper(service_engine, user_id, walk):
    trials.set_trials(service_engine, user_id, True)
    session = _start(service_engine, user_id, walk)["session_id"]

    reply = _answer(service_engine, user_id, session, walk["real"][0], flagged=False)

    [trial] = _trials(service_engine)
    assert (trial.session_id, trial.practice_item_id, trial.topic_id) == (
        session,
        walk["real"][0],
        walk["topic"],
    )
    assert (trial.was_generated, trial.flagged, trial.tell) == (0, 0, None)
    assert reply["revealed"] == [
        {
            "practice_item_id": walk["real"][0],
            "origin": "past-exam",
            "answer_provenance": "none",
            "paper_date": "2018-07-21",
        }
    ]
    assert _count(service_engine, attempt) == 1


def test_8_the_generated_item_writes_its_trial_with_the_tell(service_engine, user_id, walk):
    trials.set_trials(service_engine, user_id, True)
    session = _start(service_engine, user_id, walk)["session_id"]

    reply = _answer(
        service_engine, user_id, session, walk["generated"], flagged=True, tell="phrasing"
    )

    [trial] = _trials(service_engine)
    assert (trial.was_generated, trial.flagged, trial.tell) == (1, 1, "phrasing")
    assert reply["revealed"][0]["origin"] == "generated"
    assert reply["revealed"][0]["paper_date"] is None


def test_9_trials_on_an_answer_without_the_guess_is_refused(service_engine, user_id, walk):
    trials.set_trials(service_engine, user_id, True)
    session = _start(service_engine, user_id, walk)["session_id"]

    with pytest.raises(StudyError) as err:
        _answer(service_engine, user_id, session, walk["real"][0])

    assert err.value.code == "guess_missing"
    assert err.value.field_errors[0]["field"] == "attempts[0].flagged"
    assert "real or generated" in err.value.fix
    assert _count(service_engine, attempt) == 0  # one write unit: the attempt rolled back too
    assert _count(service_engine, discrimination_trial) == 0


def test_10_trials_off_a_guess_sent_anyway_is_ignored(service_engine, user_id, walk):
    session = _start(service_engine, user_id, walk)["session_id"]

    _answer(service_engine, user_id, session, walk["real"][0], flagged=True, tell="too-easy")

    assert _count(service_engine, attempt) == 1
    assert _count(service_engine, discrimination_trial) == 0


def test_11_trials_off_midway_the_hidden_item_is_still_revealed(service_engine, user_id, walk):
    trials.set_trials(service_engine, user_id, True)
    session = _start(service_engine, user_id, walk)["session_id"]
    trials.set_trials(service_engine, user_id, False)  # turned off halfway (D-316)

    reply = _answer(service_engine, user_id, session, walk["generated"])

    assert reply["revealed"][0]["origin"] == "generated"
    assert _count(service_engine, discrimination_trial) == 0


def test_12_a_second_answer_keeps_the_first_trial(service_engine, user_id, walk):
    trials.set_trials(service_engine, user_id, True)
    session = _start(service_engine, user_id, walk)["session_id"]
    _answer(service_engine, user_id, session, walk["generated"], flagged=False)

    # the second try sends no guess: the item is revealed now, so a guess would be no test
    _answer(service_engine, user_id, session, walk["generated"])

    [trial] = _trials(service_engine)
    assert trial.flagged == 0
    assert _count(service_engine, attempt) == 2


def test_13_a_topic_only_answer_writes_no_trial(service_engine, user_id, walk):
    trials.set_trials(service_engine, user_id, True)
    session = _start(service_engine, user_id, walk)["session_id"]

    reply = submit_attempts(
        service_engine, user_id, "I3302", [{"topic_id": walk["topic"], "correct": True}], session
    )

    assert _count(service_engine, attempt) == 1
    assert _count(service_engine, discrimination_trial) == 0
    assert "revealed" not in reply


@pytest.mark.parametrize(
    ("guess", "field"),
    [
        ({"flagged": True, "tell": "too-hard"}, "attempts[0].tell"),
        ({"tell": "phrasing"}, "attempts[0].tell"),
    ],
)
def test_14_a_bad_tell_is_refused(service_engine, user_id, walk, guess, field):
    trials.set_trials(service_engine, user_id, True)
    session = _start(service_engine, user_id, walk)["session_id"]

    with pytest.raises(StudyError) as err:
        _answer(service_engine, user_id, session, walk["real"][0], **guess)

    assert err.value.code == "invalid_value"
    assert err.value.field_errors[0]["field"] == field
    assert _count(service_engine, attempt) == 0


def test_15_trials_on_a_practice_session_writes_no_trial(service_engine, user_id, walk):
    trials.set_trials(service_engine, user_id, True)
    session = _start(service_engine, user_id, walk, mode="practice")["session_id"]

    reply = _answer(service_engine, user_id, session, walk["generated"], flagged=True)

    assert _count(service_engine, discrimination_trial) == 0
    assert "revealed" not in reply


# --- the switch: one service, two front doors (D-313, D-317) --------------------------------


def _switch(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(select(user.c.trials_on)).scalar_one()


def test_16_the_tool_and_the_route_set_the_same_switch(service_engine, user_id, call):
    assert _switch(service_engine) == 0  # off by default (D-312)

    on = call("study_set_trials", {"on": True})
    assert not on.is_error
    assert json.loads(on.content[0].text) == {"trials_on": True}
    assert _switch(service_engine) == 1

    client = TestClient(create_app(service_engine), base_url=OWN)
    off = client.post("/trials", json={"on": False})
    assert off.status_code == 200
    assert off.json() == {"trials_on": False}
    assert _switch(service_engine) == 0

    wrong = client.post("/trials", json={"on": "maybe"})
    assert wrong.status_code == 400
    assert wrong.json()["field_errors"][0]["field"] == "on"


def test_the_other_user_keeps_their_own_switch(service_engine, user_id, other_user_id):
    trials.set_trials(service_engine, user_id, True)

    with service_engine.connect() as conn:
        rows = dict(conn.execute(select(user.c.id, user.c.trials_on)).all())
    assert rows == {user_id: 1, other_user_id: 0}


def test_an_unknown_user_cannot_set_the_switch(service_engine, user_id):
    with pytest.raises(StudyError) as err:
        trials.set_trials(service_engine, new_id(), True)
    assert err.value.code == "not_found"
