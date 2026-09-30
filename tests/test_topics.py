"""Topics and coverage (1.12; D-60 - D-65): which assessments a topic is examined in, and the
edges that say so."""

import json

import pytest
from sqlalchemy import insert, select, update

from studysystem.db.tables import assessment, coverage, practice_item, topic
from studysystem.errors import StudyError
from studysystem.services.assessments import add_assessment
from studysystem.services.courses import add_course
from studysystem.services.generation import start_generation, submit_generation
from studysystem.services.ids import new_id, now
from studysystem.services.past_exams import add_past_exam
from studysystem.services.topics import (
    confirm_topic_proposal,
    edge_if_missing,
    exam_assessments,
    get_topics,
)
from studysystem.services.units import write_unit
from tests._papers import pdf_pages, write

SEM = "Semester 1 2026-2027"
REGEX = "Regular expressions"
FORMS = "PHP forms"


def item(position, name) -> dict:
    return {
        "position": position,
        "question": f"Question at position {position}",
        "answer_key": None,
        "marks": None,
        "topic": {"proposed_name": name},
    }


@pytest.fixture
def web(service_engine, user_id):
    return add_course(service_engine, user_id, "I3302", "Server-Side Web Development", SEM)


def transcribe(engine, user_id, tmp_path, session_type, date, names, slot="Final exam") -> str:
    """Register a paper in one of I3302's slots and transcribe it: one item per name, in order."""
    path = write(tmp_path / date, f"I3302_{date}.pdf", pdf_pages([f"paper of {date}"]))
    paper_id = add_past_exam(engine, user_id, "I3302", slot, session_type, date, path, "Dr. Hamze")[
        "past_exam_id"
    ]
    task_id = start_generation(engine, user_id, "transcription", paper_id).payload["task_id"]
    items = [item(p, n) for p, n in enumerate(names, start=1)]
    submit_generation(engine, user_id, task_id, {"items": items})
    return paper_id


def topic_id(engine, name) -> str:
    with engine.connect() as conn:
        return conn.execute(select(topic.c.id).where(topic.c.name == name)).scalar_one()


def assessments_of(engine, topic_name) -> list[str]:
    with engine.connect() as conn:
        return exam_assessments(conn, topic_id(engine, topic_name))


# --- exam_assessments (D-65) --------------------------------------------------------


def test_three_items_from_one_paper_give_one_assessment(service_engine, user_id, web, tmp_path):
    transcribe(service_engine, user_id, tmp_path, "first", "2020-02-17", [REGEX] * 3)

    assert assessments_of(service_engine, REGEX) == [web["assessment_id"]]


def test_items_in_two_papers_of_one_slot_give_one_assessment(
    service_engine, user_id, web, tmp_path
):
    transcribe(service_engine, user_id, tmp_path, "first", "2020-02-17", [FORMS])
    transcribe(service_engine, user_id, tmp_path, "second", "2021-09-20", [FORMS])

    assert assessments_of(service_engine, FORMS) == [web["assessment_id"]]


def test_every_sitting_in_the_slot_is_returned(service_engine, user_id, web, tmp_path):
    """A resit paper is evidence for the same exam, so both sittings of the slot count."""
    resit = new_id()
    with write_unit(service_engine) as conn:
        conn.execute(
            insert(assessment).values(
                id=resit,
                user_id=user_id,
                slot_id=web["slot_id"],
                weight=None,
                weight_tier="unknown",
                session_type="second",
                created_at=now(),
            )
        )
    transcribe(service_engine, user_id, tmp_path, "first", "2020-02-17", [REGEX])

    assert sorted(assessments_of(service_engine, REGEX)) == sorted([web["assessment_id"], resit])


def test_a_topic_with_no_past_exam_item_gives_none(service_engine, user_id, web):
    tid = new_id()
    with write_unit(service_engine) as conn:
        conn.execute(
            insert(topic).values(
                id=tid,
                user_id=user_id,
                course_id=web["course_id"],
                name="Material-only topic",
                status="active",
                proposed_by="material",
                status_changed_at=now(),
                created_at=now(),
            )
        )
        assert exam_assessments(conn, tid) == []


# --- edge_if_missing (D-60) ---------------------------------------------------------


def edges(engine) -> list:
    with engine.connect() as conn:
        return [r._mapping for r in conn.execute(select(coverage))]


def test_an_edge_is_inferred_and_unconfirmed(service_engine, user_id, web, tmp_path):
    transcribe(service_engine, user_id, tmp_path, "first", "2020-02-17", [REGEX])
    regex = topic_id(service_engine, REGEX)
    with write_unit(service_engine) as conn:
        edge_if_missing(conn, user_id, regex, web["assessment_id"])

    (edge,) = edges(service_engine)
    assert (edge["topic_id"], edge["assessment_id"], edge["material_id"]) == (
        regex,
        web["assessment_id"],
        None,
    )
    assert (edge["tier"], edge["confirmed_at"]) == ("inferred", None)


def test_a_second_call_for_the_same_pair_writes_nothing(service_engine, user_id, web, tmp_path):
    transcribe(service_engine, user_id, tmp_path, "first", "2020-02-17", [REGEX])
    regex = topic_id(service_engine, REGEX)
    for _ in range(2):
        with write_unit(service_engine) as conn:
            edge_if_missing(conn, user_id, regex, web["assessment_id"])

    assert len(edges(service_engine)) == 1


# --- submit writes edges for active topics only (D-60) ------------------------------


def test_an_item_on_an_active_topic_from_a_new_slot_gets_its_edge(
    service_engine, user_id, web, tmp_path
):
    """Paper 4 case: regex is already active, and a Partial-exam paper tags an item to it."""
    transcribe(service_engine, user_id, tmp_path, "first", "2020-02-17", [REGEX])
    regex = topic_id(service_engine, REGEX)
    with write_unit(service_engine) as conn:
        conn.execute(update(topic).where(topic.c.id == regex).values(status="active"))
    partial = add_assessment(service_engine, user_id, "I3302", "Partial exam", "exam")
    transcribe(service_engine, user_id, tmp_path, "first", "2021-11-15", [REGEX], "Partial exam")

    assert {e["assessment_id"] for e in edges(service_engine) if e["topic_id"] == regex} == {
        web["assessment_id"],
        partial["assessment_id"],
    }


def test_an_item_on_a_proposed_topic_gets_no_edge(service_engine, user_id, web, tmp_path):
    transcribe(service_engine, user_id, tmp_path, "first", "2020-02-17", [REGEX, FORMS])

    assert edges(service_engine) == []


# --- confirm_topic_proposal: accept (D-60) ------------------------------------------


def status_of(engine, tid) -> str:
    with engine.connect() as conn:
        return conn.execute(select(topic.c.status).where(topic.c.id == tid)).scalar_one()


def refused(engine, user_id, tid, decision, retag=None) -> StudyError:
    with pytest.raises(StudyError) as excinfo:
        confirm_topic_proposal(engine, user_id, tid, decision, retag)
    return excinfo.value


def test_accept_makes_the_topic_active_with_its_edge(service_engine, user_id, web, tmp_path):
    transcribe(service_engine, user_id, tmp_path, "first", "2020-02-17", [REGEX] * 3)
    regex = topic_id(service_engine, REGEX)

    reply = confirm_topic_proposal(service_engine, user_id, regex, "accept")

    assert reply == {
        "topic_id": regex,
        "name": REGEX,
        "status": "active",
        "examined_in": [web["assessment_id"]],
    }
    assert status_of(service_engine, regex) == "active"
    assert [(e["topic_id"], e["assessment_id"]) for e in edges(service_engine)] == [
        (regex, web["assessment_id"])
    ]


def test_accepting_twice_is_refused_and_writes_nothing(service_engine, user_id, web, tmp_path):
    transcribe(service_engine, user_id, tmp_path, "first", "2020-02-17", [REGEX])
    regex = topic_id(service_engine, REGEX)
    confirm_topic_proposal(service_engine, user_id, regex, "accept")

    err = refused(service_engine, user_id, regex, "accept")

    assert err.code == "not_proposed"
    assert len(edges(service_engine)) == 1


def test_an_unknown_topic_is_not_found(service_engine, user_id, web):
    assert refused(service_engine, user_id, "X" * 26, "accept").code == "not_found"


def test_another_users_topic_is_not_found(service_engine, user_id, other_user_id, web, tmp_path):
    transcribe(service_engine, user_id, tmp_path, "first", "2020-02-17", [REGEX])
    regex = topic_id(service_engine, REGEX)

    assert refused(service_engine, other_user_id, regex, "accept").code == "not_found"
    assert status_of(service_engine, regex) == "proposed"


def test_an_accept_sent_with_a_retag_is_refused(service_engine, user_id, web, tmp_path):
    transcribe(service_engine, user_id, tmp_path, "first", "2020-02-17", [REGEX])
    regex = topic_id(service_engine, REGEX)

    err = refused(service_engine, user_id, regex, "accept", retag=[])

    assert (err.code, err.field_errors[0]["field"]) == ("invalid_value", "retag")
    assert status_of(service_engine, regex) == "proposed"


def test_a_decision_that_is_not_accept_or_decline_is_refused(
    service_engine, user_id, web, tmp_path
):
    transcribe(service_engine, user_id, tmp_path, "first", "2020-02-17", [REGEX])
    regex = topic_id(service_engine, REGEX)

    err = refused(service_engine, user_id, regex, "approve")

    assert (err.code, err.field_errors[0]["field"]) == ("invalid_value", "decision")


# --- confirm_topic_proposal: decline (D-61 - D-63) ----------------------------------

SESSIONS = "PHP sessions & associative arrays"
FILES = "PHP files & directories"


def item_ids(engine, name) -> list[str]:
    """The ids of a topic's items, in paper order."""
    with engine.connect() as conn:
        return list(
            conn.execute(
                select(practice_item.c.id)
                .join(topic, topic.c.id == practice_item.c.topic_id)
                .where(topic.c.name == name)
                .order_by(practice_item.c.position)
            ).scalars()
        )


def item_topic(engine, iid) -> str:
    with engine.connect() as conn:
        return conn.execute(
            select(topic.c.name)
            .join(practice_item, practice_item.c.topic_id == topic.c.id)
            .where(practice_item.c.id == iid)
        ).scalar_one()


@pytest.fixture
def paper2(service_engine, user_id, web, tmp_path):
    """Paper 2's shape: four file questions and one array question under the mixed topic, and
    one question already under files & directories."""
    transcribe(service_engine, user_id, tmp_path, "second", "2020-09-14", [SESSIONS] * 5 + [FILES])
    return topic_id(service_engine, SESSIONS)


def test_a_decline_spreads_items_over_existing_and_new_topics(service_engine, user_id, paper2):
    q1, q2, q3, q4, q7 = item_ids(service_engine, SESSIONS)
    retag = [
        {"item_id": i, "topic": {"proposed_name": "php FILES & directories"}} for i in (q1, q2)
    ]
    retag += [{"item_id": i, "topic": {"proposed_name": "PHP sessions"}} for i in (q3, q4)]
    retag += [{"item_id": q7, "topic": {"proposed_name": "Associative arrays"}}]

    reply = confirm_topic_proposal(service_engine, user_id, paper2, "decline", retag)

    assert reply["status"] == "declined"
    assert status_of(service_engine, paper2) == "declined"
    assert [item_topic(service_engine, i) for i in (q1, q2, q3, q4, q7)] == [
        FILES,
        FILES,
        "PHP sessions",
        "PHP sessions",
        "Associative arrays",
    ]
    assert item_ids(service_engine, SESSIONS) == []  # 1.12's Done when
    assert status_of(service_engine, topic_id(service_engine, "PHP sessions")) == "proposed"
    assert edges(service_engine) == []  # every target is still proposed (D-60)


def test_an_active_target_gets_its_edge(service_engine, user_id, web, paper2):
    files = topic_id(service_engine, FILES)
    confirm_topic_proposal(service_engine, user_id, files, "accept")
    retag = [{"item_id": i, "topic": {"id": files}} for i in item_ids(service_engine, SESSIONS)]

    reply = confirm_topic_proposal(service_engine, user_id, paper2, "decline", retag)

    assert {r["topic_name"] for r in reply["retagged"]} == {FILES}
    assert [(e["topic_id"], e["assessment_id"]) for e in edges(service_engine)] == [
        (files, web["assessment_id"])
    ]


def test_missing_foreign_and_duplicate_items_are_named_in_one_error(
    service_engine, user_id, paper2
):
    q1, q2, q3, q4, _q7 = item_ids(service_engine, SESSIONS)
    (outsider,) = item_ids(service_engine, FILES)
    to = {"proposed_name": "PHP sessions"}
    retag = [{"item_id": i, "topic": to} for i in (q1, q1, q2, q3, q4, outsider)]

    err = refused(service_engine, user_id, paper2, "decline", retag)

    problems = " | ".join(f["problem"] for f in err.field_errors)
    assert err.code == "invalid_value"
    assert f"missing: {_q7}" in problems
    assert f"not this topic's item: {outsider}" in problems
    assert f"listed twice: {q1}" in problems
    assert status_of(service_engine, paper2) == "proposed"


@pytest.mark.parametrize("by", ["id", "name"])
def test_a_target_that_is_the_declined_topic_is_refused(service_engine, user_id, paper2, by):
    to = {"id": paper2} if by == "id" else {"proposed_name": SESSIONS.upper()}
    retag = [{"item_id": i, "topic": to} for i in item_ids(service_engine, SESSIONS)]

    err = refused(service_engine, user_id, paper2, "decline", retag)

    assert err.field_errors[0]["problem"] == "is the topic being declined"
    assert status_of(service_engine, paper2) == "proposed"


def test_a_target_id_that_is_not_a_live_topic_is_refused(service_engine, user_id, paper2):
    retag = [{"item_id": i, "topic": {"id": "X" * 26}} for i in item_ids(service_engine, SESSIONS)]

    err = refused(service_engine, user_id, paper2, "decline", retag)

    assert err.field_errors[0]["problem"] == "not a live topic of this course"


def test_a_refused_decline_leaves_no_new_topic_behind(service_engine, user_id, paper2):
    """A new name resolved before a later entry fails is rolled back with the unit."""
    q1, q2, q3, q4, q7 = item_ids(service_engine, SESSIONS)
    retag = [{"item_id": i, "topic": {"proposed_name": "Brand new"}} for i in (q1, q2, q3, q4)]
    retag += [{"item_id": q7, "topic": {"id": paper2}}]

    refused(service_engine, user_id, paper2, "decline", retag)

    with service_engine.connect() as conn:
        assert conn.execute(select(topic).where(topic.c.name == "Brand new")).all() == []


def test_a_decline_without_retag_is_refused(service_engine, user_id, paper2):
    err = refused(service_engine, user_id, paper2, "decline")

    assert (err.code, err.field_errors[0]["field"]) == ("invalid_value", "retag")


@pytest.mark.parametrize(
    "entry",
    [
        "not an object",
        {"topic": {"proposed_name": "PHP sessions"}},
        {"item_id": "x", "topic": {"proposed_name": "  "}},
        {"item_id": "x", "topic": {"id": "a", "proposed_name": "b"}},
    ],
)
def test_a_malformed_retag_entry_is_refused(service_engine, user_id, paper2, entry):
    err = refused(service_engine, user_id, paper2, "decline", [entry])

    assert err.code == "invalid_value"
    assert err.field_errors[0]["field"].startswith("retag[0]")


# --- get_topics (D-64, D-66) --------------------------------------------------------


def listing(engine, user_id) -> dict:
    return {t["name"]: t for t in get_topics(engine, user_id, "I3302")["topics"]}


def test_live_topics_list_their_items_in_paper_order(service_engine, user_id, web, tmp_path):
    transcribe(service_engine, user_id, tmp_path, "second", "2021-09-20", [FORMS])
    transcribe(service_engine, user_id, tmp_path, "first", "2020-02-17", [REGEX, FORMS])

    topics = listing(service_engine, user_id)

    assert set(topics) == {REGEX, FORMS}
    forms = topics[FORMS]
    assert forms["status"] == "proposed"
    assert [(i["paper_date"], i["position"]) for i in forms["items"]] == [
        ("2020-02-17", 2),
        ("2021-09-20", 1),
    ]
    assert forms["items"][0]["question"] == "Question at position 2"  # the full text (D-66)
    assert forms["items"][0]["id"] in item_ids(service_engine, FORMS)


def test_a_declined_topic_is_not_listed(service_engine, user_id, web, tmp_path):
    transcribe(service_engine, user_id, tmp_path, "first", "2020-02-17", [REGEX, FORMS])
    forms = topic_id(service_engine, FORMS)
    (only,) = item_ids(service_engine, FORMS)
    confirm_topic_proposal(
        service_engine,
        user_id,
        forms,
        "decline",
        [{"item_id": only, "topic": {"proposed_name": REGEX}}],
    )

    assert set(listing(service_engine, user_id)) == {REGEX}


def test_an_item_with_no_paper_is_listed_without_a_date(service_engine, user_id, web, tmp_path):
    """A generated item has no paper; the listing must still show it, or a decline could never
    name it and would fail with "missing"."""
    transcribe(service_engine, user_id, tmp_path, "first", "2020-02-17", [REGEX])
    regex = topic_id(service_engine, REGEX)
    with service_engine.connect() as conn:
        task = conn.execute(select(practice_item.c.task_id)).scalars().first()
    with write_unit(service_engine) as conn:
        conn.execute(
            insert(practice_item).values(
                id=new_id(),
                user_id=user_id,
                topic_id=regex,
                task_id=task,
                item_ordinal=99,
                origin="generated",
                question="A generated regex question",
                answer_provenance="none",
                source_marker="model-knowledge",
                novelty_score=0.5,
                created_at=now(),
            )
        )

    items = listing(service_engine, user_id)[REGEX]["items"]

    assert [(i["paper_date"], i["position"]) for i in items] == [("2020-02-17", 1), (None, None)]


def test_an_unknown_course_is_not_found(service_engine, user_id, web):
    with pytest.raises(StudyError) as excinfo:
        get_topics(service_engine, user_id, "NOPE101")
    assert excinfo.value.code == "not_found"


def test_a_course_with_no_topics_lists_none(service_engine, user_id, web):
    assert get_topics(service_engine, user_id, "I3302") == {"course": "I3302", "topics": []}


# --- the two tools, through a real client -------------------------------------------


def test_list_then_accept_and_decline_through_the_tools(
    call, service_engine, user_id, web, tmp_path
):
    transcribe(service_engine, user_id, tmp_path, "first", "2020-02-17", [REGEX, FORMS])

    listed = json.loads(call("study_get_topics", {"code": "I3302"}).content[0].text)
    by_name = {t["name"]: t for t in listed["topics"]}
    regex, forms = by_name[REGEX], by_name[FORMS]

    accepted = call("study_confirm_topic_proposal", {"topic_id": regex["id"], "decision": "accept"})
    retag = [{"item_id": i["id"], "topic": {"id": regex["id"]}} for i in forms["items"]]
    declined = call(
        "study_confirm_topic_proposal",
        {"topic_id": forms["id"], "decision": "decline", "retag": retag},
    )

    assert not accepted.is_error and not declined.is_error
    assert json.loads(declined.content[0].text)["status"] == "declined"
    assert item_ids(service_engine, FORMS) == []


def test_a_refused_decision_reaches_the_host_as_an_error_result(call, user_id, web):
    result = call("study_confirm_topic_proposal", {"topic_id": "X" * 26, "decision": "accept"})

    assert result.is_error
    assert json.loads(result.content[0].text)["code"] == "not_found"
