"""The observation task (2.1a; D-149, D-151, D-154): I3302's 2023-09 resit leaves as its pages and
comes back as claims - its 90 minutes, its question count, the traps Ghorayeb set. Each claim is
checked alone against the schema, then all of them together against the paper (`judge_claims`):
a format key once per paper, an instructor claim once per text. Each kept claim is one
`observation` row and its `evidence` row."""

import json

import pytest
from jsonschema import Draft202012Validator
from sqlalchemy import func, select, update

from studysystem.db.tables import evidence, generation_task, observation, past_exam
from studysystem.errors import StudyError
from studysystem.services.claims import judge_claims
from studysystem.services.generation import (
    OBSERVATION_SCHEMA,
    OBSERVATION_STEPS,
    start_generation,
    submit_generation,
)
from studysystem.services.past_exams import add_past_exam
from studysystem.services.units import write_unit
from tests._papers import pdf_pages, write
from tests.test_profiles import FINAL, web  # noqa: F401 - fixture


def duration(minutes, citation="header"):
    return {
        "claim_key": "duration-minutes",
        "claim_value": {"minutes": minutes},
        "citation": citation,
    }


def count(n):
    return {"claim_key": "question-count", "claim_value": {"count": n}}


def trap(text, citation="Q2"):
    return {"claim_key": "trap", "claim_value": {"text": text}, "citation": citation}


EQUALS = "comparing a form value with == where === is needed"
GET = "a query built from $_GET without a prepared statement"


def stored(*claims):
    """Claims as the database hands them back: key and value, no citation."""
    return [{"claim_key": c["claim_key"], "claim_value": c["claim_value"]} for c in claims]


def placed(*claims):
    """(place, claim) pairs, places from 1 - what the submit hands `judge_claims`."""
    return list(enumerate(claims, start=1))


# --- judge_claims: your piece (D-154) -------------------------------------------------------


def test_distinct_claims_are_all_accepted():
    """2023-09 as it should come back: one duration, one count, two different traps."""
    got = judge_claims(placed(duration(90), count(4), trap(EQUALS), trap(GET, "Q4")), [])

    assert (got.accepted, got.skipped, got.errors) == ([1, 2, 3, 4], [], [])


def test_a_format_key_twice_is_refused_on_both_copies():
    """120 or 90? The server cannot tell, so neither is kept; the trap between them is."""
    got = judge_claims(placed(duration(120), trap(EQUALS), duration(90)), [])

    assert (got.accepted, got.skipped) == ([2], [])
    assert [(e["item_ordinal"], e["code"], e["field"]) for e in got.errors] == [
        (1, "duplicate_claim", "claim_key"),
        (3, "duplicate_claim", "claim_key"),
    ]


def test_the_same_trap_text_twice_is_one_claim():
    """Same key, same text: the second copy says nothing new - skipped, never an error."""
    got = judge_claims(placed(trap(EQUALS, "Q2"), trap(EQUALS, "Q4")), [])

    assert (got.accepted, got.skipped, got.errors) == ([1], [2], [])


def test_the_same_text_under_two_keys_is_two_claims():
    phrasing = {"claim_key": "phrasing", "claim_value": {"text": EQUALS}}

    got = judge_claims(placed(trap(EQUALS), phrasing), [])

    assert (got.accepted, got.skipped, got.errors) == ([1, 2], [], [])


def test_a_format_key_already_stored_is_skipped_whatever_its_value():
    """Submission 1 kept 90; a resend carrying the key again rewrites nothing (D-52)."""
    got = judge_claims(placed(duration(120), count(4)), stored(duration(90)))

    assert (got.accepted, got.skipped, got.errors) == ([2], [1], [])


def test_an_instructor_claim_already_stored_is_skipped_by_its_text():
    got = judge_claims(placed(trap(EQUALS), trap(GET)), stored(trap(EQUALS)))

    assert (got.accepted, got.skipped, got.errors) == ([2], [1], [])


def test_a_stored_format_key_sent_twice_is_skipped_not_refused():
    """The paper already has its one duration - nothing in this payload can be written."""
    got = judge_claims(placed(duration(90), duration(120)), stored(duration(90)))

    assert (got.accepted, got.skipped, got.errors) == ([], [1, 2], [])


def test_no_claims_sort_into_nothing():
    got = judge_claims([], stored(duration(90)))

    assert (got.accepted, got.skipped, got.errors) == ([], [], [])


# --- start (plumbing) -----------------------------------------------------------------------

HEADER = "Final Exam - Server-Side Web Development - Dr. Ali Ghorayeb - Duration: 90 min"
PAGES = [HEADER, "Question 1 ...", "Question 4 ..."]


def add_paper(engine, user_id, folder, pages=PAGES) -> str:
    path = write(folder, "I3302_2023-09-13.pdf", pdf_pages(pages))
    return add_past_exam(
        engine,
        user_id,
        "I3302",
        FINAL,
        "second",
        "2023-09-13",
        path,
        instructor="Dr. Ali Ghorayeb",
    )["past_exam_id"]


@pytest.fixture
def paper(service_engine, user_id, web, tmp_path):  # noqa: F811
    return add_paper(service_engine, user_id, tmp_path)


def start(engine, user_id, paper_id):
    return start_generation(engine, user_id, "observation", past_exam_id=paper_id)


def submit(engine, user_id, task_id, *claims):
    return submit_generation(engine, user_id, task_id, {"claims": list(claims)})


@pytest.fixture
def task_id(service_engine, user_id, paper):
    return start(service_engine, user_id, paper).payload["task_id"]


def refused(engine, user_id, paper_id) -> StudyError:
    with pytest.raises(StudyError) as excinfo:
        start(engine, user_id, paper_id)
    return excinfo.value


def tasks(engine):
    with engine.connect() as conn:
        return conn.execute(
            select(generation_task).where(generation_task.c.kind == "observation")
        ).all()


def observations(engine):
    with engine.connect() as conn:
        return conn.execute(select(observation).order_by(observation.c.id)).all()


def test_the_paper_leaves_as_its_pages_with_the_schema_and_steps(service_engine, user_id, paper):
    payload = start(service_engine, user_id, paper).payload

    assert [p["text"] for p in payload["content"]] == PAGES
    assert payload["schema"] == OBSERVATION_SCHEMA
    assert payload["rules"] == {"steps": OBSERVATION_STEPS}
    (row,) = tasks(service_engine)
    assert (row.id, row.scope_type, row.scope_id, row.exam_profile_id, row.status) == (
        payload["task_id"],
        "past-exam",
        paper,
        None,
        "open",
    )


def test_the_host_reads_the_value_shape_from_the_key(service_engine, user_id, paper):
    host = Draft202012Validator(
        json.loads(json.dumps(start(service_engine, user_id, paper).payload["schema"]))
    )

    assert host.is_valid({"claims": [duration(90), count(4), trap(EQUALS)]})
    assert not host.is_valid({"claims": []})
    assert not host.is_valid({"claims": [{**duration(90), "claim_value": {"text": "90 min"}}]})
    topic_marks = {"claim_key": "topic-marks", "claim_value": {"marks": 12, "of_total": 100}}
    assert not host.is_valid({"claims": [topic_marks]})  # D-151


def test_starting_again_hands_back_the_same_open_task(service_engine, user_id, paper):
    first = start(service_engine, user_id, paper).payload["task_id"]

    assert start(service_engine, user_id, paper).payload["task_id"] == first
    assert len(tasks(service_engine)) == 1


def test_an_observation_needs_a_paper(service_engine, user_id, web):  # noqa: F811
    err = refused(service_engine, user_id, None)

    assert (err.code, err.field_errors[0]["field"]) == ("invalid_value", "past_exam_id")


def test_an_unknown_paper_is_not_found(service_engine, user_id, web):  # noqa: F811
    assert refused(service_engine, user_id, "01M3NOPE00000000000000000Z").code == "not_found"


def test_another_students_paper_is_not_found(service_engine, other_user_id, paper):
    assert refused(service_engine, other_user_id, paper).code == "not_found"


def test_a_scanned_paper_waits_for_the_image_path(service_engine, user_id, web, tmp_path):  # noqa: F811
    scan = add_paper(service_engine, user_id, tmp_path, pages=[None])

    assert refused(service_engine, user_id, scan).code == "not_supported_yet"


def test_an_open_task_that_kept_claims_is_fetched_again(service_engine, user_id, paper, task_id):
    """QA's case: submission 1 kept the duration, the chat lost the payload - the task still has
    two submissions left, so its pages must come back (D-46)."""
    submit(service_engine, user_id, task_id, duration(90), trap("  "))

    assert start(service_engine, user_id, paper).payload["task_id"] == task_id


def test_a_paper_that_holds_observations_is_refused(service_engine, user_id, paper, task_id):
    submit(service_engine, user_id, task_id, duration(90))

    err = refused(service_engine, user_id, paper)

    assert (err.code, err.field_errors[0]["field"]) == ("already_observed", "past_exam_id")


def test_each_start_logs_one_json_line(service_engine, user_id, paper, caplog):
    with caplog.at_level("INFO", logger="studysystem.services.generation"):
        start(service_engine, user_id, paper)

    line = json.loads(caplog.records[-1].getMessage())
    assert (line["event"], line["kind"], line["past_exam_id"]) == (
        "start_generation",
        "observation",
        paper,
    )


# --- submit (plumbing around judge_claims) --------------------------------------------------


def test_each_kept_claim_is_one_observation_and_its_evidence(
    service_engine, user_id, paper, task_id
):
    reply = submit(
        service_engine, user_id, task_id, duration(90), count(4), trap(EQUALS), trap(GET, "Q4")
    )

    assert (reply["status"], reply["accepted"], reply["item_errors"]) == (
        "closed",
        [1, 2, 3, 4],
        [],
    )
    with service_engine.connect() as conn:
        slot_id = conn.execute(
            select(past_exam.c.slot_id).where(past_exam.c.id == paper)
        ).scalar_one()
    rows = observations(service_engine)
    kept = [(r.claim_key, r.claim_class, json.loads(r.claim_value), r.citation) for r in rows]
    assert sorted(kept, key=lambda k: (k[0], k[3] or "")) == [
        ("duration-minutes", "format", {"minutes": 90}, "header"),
        ("question-count", "format", {"count": 4}, None),
        ("trap", "instructor-driven", {"text": EQUALS}, "Q2"),
        ("trap", "instructor-driven", {"text": GET}, "Q4"),
    ]
    assert {(r.past_exam_id, r.slot_id, r.topic_id, r.study_tier, r.owner_id) for r in rows} == {
        (paper, slot_id, None, "E1", user_id)
    }
    with service_engine.connect() as conn:
        links = conn.execute(select(evidence.c.observation_id, evidence.c.study_tier)).all()
    assert sorted(links) == sorted((r.id, "E1") for r in rows)


BAD_CLAIMS = [  # (claim, field, code) - one bad claim, sent beside a good one
    ({**duration(90), "claim_value": {"text": "90 min"}}, "claim_value", "required"),
    (duration("ninety"), "claim_value", "type"),
    (duration(-5), "claim_value", "minimum"),
    ({"claim_key": "total-marks", "claim_value": {"marks": 0}}, "claim_value", "exclusiveMinimum"),
    ({"claim_key": "has-mcq", "claim_value": {"value": "yes"}}, "claim_value", "type"),
    (trap("   "), "claim_value", "pattern"),
    ({"claim_key": "difficulty", "claim_value": {"text": "hard"}}, "claim_key", "enum"),
    (
        {"claim_key": "topic-marks", "claim_value": {"marks": 12, "of_total": 100}},
        "claim_key",
        "topic_marks_refused",
    ),
    ({**count(4), "confidence": "high"}, "confidence", "additionalProperties"),
]


@pytest.mark.parametrize(("claim", "field", "code"), BAD_CLAIMS)
def test_a_bad_claim_fails_alone(service_engine, user_id, task_id, claim, field, code):
    reply = submit(service_engine, user_id, task_id, trap(EQUALS), claim)

    assert (reply["status"], reply["accepted"]) == ("open", [1])
    assert (field, code) in {(e["field"], e["code"]) for e in reply["item_errors"]}
    assert {e["item_ordinal"] for e in reply["item_errors"]} == {2}
    assert [r.claim_key for r in observations(service_engine)] == ["trap"]


def test_a_payload_without_claims_is_an_envelope_error(service_engine, user_id, task_id):
    reply = submit_generation(service_engine, user_id, task_id, {"claims": []})

    assert reply["status"] == "open"
    assert [e["item_ordinal"] for e in reply["item_errors"]] == [None]
    assert observations(service_engine) == []


def test_two_durations_keep_neither(service_engine, user_id, task_id):
    reply = submit(service_engine, user_id, task_id, duration(120), count(4), duration(90))

    assert (reply["status"], reply["accepted"]) == ("open", [2])
    assert [r.claim_key for r in observations(service_engine)] == ["question-count"]


def test_a_resend_skips_what_is_kept_and_closes(service_engine, user_id, task_id):
    first = submit(service_engine, user_id, task_id, duration(90), trap(EQUALS), trap("  "))
    assert (first["status"], first["accepted"]) == ("open", [1, 2])

    second = submit(service_engine, user_id, task_id, duration(90), trap(EQUALS), trap(GET))

    assert (second["status"], second["accepted"], second["skipped"]) == ("closed", [3], [1, 2])
    assert len(observations(service_engine)) == 3


def test_a_third_failed_submission_closes_the_task_partial(service_engine, user_id, task_id):
    replies = [submit(service_engine, user_id, task_id, duration(90), trap("  ")) for _ in range(3)]
    reply = replies[-1]

    assert (reply["status"], reply["submissions_left"]) == ("partial", 0)
    assert [r.claim_key for r in observations(service_engine)] == ["duration-minutes"]


def test_a_partial_task_that_kept_a_claim_still_refuses_a_second(
    service_engine, user_id, paper, task_id
):
    submit(service_engine, user_id, task_id, duration(90), trap("  "))
    with write_unit(service_engine) as conn:
        conn.execute(
            update(generation_task)
            .where(generation_task.c.id == task_id)
            .values(status="partial", closed_at="2026-10-13T00:00:00Z")
        )

    assert refused(service_engine, user_id, paper).code == "already_observed"


def test_each_submit_logs_one_json_line(service_engine, user_id, task_id, caplog):
    with caplog.at_level("INFO", logger="studysystem.services.generation"):
        submit(service_engine, user_id, task_id, duration(90), trap("  "))

    line = json.loads(caplog.records[-1].getMessage())
    assert (line["event"], line["status"], line["accepted"], line["rejected"]) == (
        "submit_generation",
        "open",
        1,
        1,
    )


def test_the_tools_observe_a_paper(call, service_engine, paper):
    started = call("study_start_generation", {"kind": "observation", "past_exam_id": paper})
    assert not started.is_error, started.content[0].text
    task = json.loads(started.content[0].text)["task_id"]

    done = call("study_submit_generation", {"task_id": task, "payload": {"claims": [count(4)]}})

    assert not done.is_error, done.content[0].text
    assert json.loads(done.content[0].text)["status"] == "closed"
    with service_engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(observation)).scalar_one() == 1
