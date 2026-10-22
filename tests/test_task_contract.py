"""One contract every built generation kind keeps (D-46): a task still open is handed back by
start, even after a submission kept part of its answer - the host can lose the payload, and the
task still has submissions left. 2.1a broke it for observations and `qa` caught it; this test
makes each new kind sign up instead. Note joined by D-155."""

import pytest

from studysystem.services.generation import BUILT, start_generation, submit_generation
from tests.test_note_generation import ch6, i3302, web  # noqa: F401 - fixtures
from tests.test_profiles import register
from tests.test_tagging import topic_ids


def _transcription(engine, user_id, request):
    request.getfixturevalue("web")
    paper = register(engine, user_id, request.getfixturevalue("tmp_path"), "2022-01-20")
    item = {"answer_key": None, "marks": None, "topic": {"proposed_name": "PHP forms"}}
    keeps_one = {
        "items": [
            {"position": 1, "question": "Question 1", **item},
            {"position": 2, "question": "   ", **item},  # blank: an item error
        ]
    }
    return (lambda: start_generation(engine, user_id, "transcription", paper)), keeps_one


def _tagging(engine, user_id, request):
    chapter = request.getfixturevalue("ch6")
    real = next(iter(topic_ids(engine).values()))
    keeps_one = {"topics": [{"id": real}, {"id": "01M3NOPE00000000000000000Z"}]}
    return (lambda: start_generation(engine, user_id, "tagging", material_id=chapter)), keeps_one


def _note(engine, user_id, request):
    chapter = request.getfixturevalue("ch6")
    keeps_one = {
        "notes": [
            {"section": 1, "body": "# Chapter 6"},
            {"section": 2, "body": "# More"},  # one section until D-11's split: an item error
        ]
    }
    return (lambda: start_generation(engine, user_id, "note", material_id=chapter)), keeps_one


def _observation(engine, user_id, request):
    request.getfixturevalue("web")
    paper = register(engine, user_id, request.getfixturevalue("tmp_path"), "2023-09-13")
    keeps_one = {
        "claims": [
            {"claim_key": "duration-minutes", "claim_value": {"minutes": 90}},
            {"claim_key": "trap", "claim_value": {"text": "   "}},  # blank: an item error
        ]
    }
    return (lambda: start_generation(engine, user_id, "observation", paper)), keeps_one


def _practice(engine, user_id, request):
    chapter = request.getfixturevalue("ch6")
    real = next(iter(topic_ids(engine).values()))
    keeps_one = {
        "items": [
            {"question": "Q1", "answer_key": "A1", "topic_id": real, "source": "from-material"},
            {"question": "Q2", "topic_id": real, "source": "from-material"},  # no key: an error
        ]
    }
    return (lambda: start_generation(engine, user_id, "practice", material_id=chapter)), keeps_one


# kind -> builder: (start, a payload that keeps part and leaves the task open)
STARTERS = {
    "transcription": _transcription,
    "note": _note,
    "tagging": _tagging,
    "observation": _observation,
    "practice": _practice,
}


@pytest.mark.parametrize("kind", BUILT)
def test_an_open_task_that_kept_part_of_its_answer_is_handed_back(
    kind, service_engine, user_id, request
):
    assert kind in STARTERS, f"{kind} is built but has no starter here - add one (D-46)"
    start, keeps_one = STARTERS[kind](service_engine, user_id, request)
    task_id = start().payload["task_id"]

    reply = submit_generation(service_engine, user_id, task_id, keeps_one)
    assert (reply["status"], len(reply["accepted"])) == ("open", 1)

    assert start().payload["task_id"] == task_id
