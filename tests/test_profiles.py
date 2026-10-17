"""Exam profiles (1.13; D-14, D-67 - D-69). The weight math first: a pure function over papers
given newest first - no database. Then `derive_profile`, the write unit around it, its routed
claims (2.1b), its per-class `provisional` (2.2) and format by the session he sits next (2.3)."""

import json

import pytest
from sqlalchemy import func, insert, select, update

from studysystem.db.tables import (
    assessment,
    evidence,
    exam_profile,
    generation_task,
    observation,
    past_exam,
    practice_item,
    profile_claim,
    topic,
    topic_state,
    topic_weight,
)
from studysystem.errors import StudyError
from studysystem.services.assessments import add_assessment
from studysystem.services.courses import add_course, set_course_input
from studysystem.services.generation import start_generation, submit_generation
from studysystem.services.ids import new_id, now
from studysystem.services.past_exams import add_past_exam
from studysystem.services.profiles import (
    derive_profile,
    fit_statements,
    pin_weights,
    topic_weights,
)
from studysystem.services.statements import add_instructor_statement
from studysystem.services.topics import confirm_topic_proposal
from studysystem.services.units import write_unit
from tests._papers import pdf_pages, write

# I3302's three papers as transcribed (1.10), newest first; topic names stand in for ids.
PAPER_2021 = [("forms", 23.0), ("files", 22.0)] + [("mysql", None)] * 4 + [("sessions", 15.0)]
PAPER_2020_RESIT = [("files", None)] * 4 + [("cookies", None)] * 3 + [("sessions", None)]
PAPER_2020_FIRST = [("regex", None)] * 3 + [
    ("mysql", 5.0),
    ("mysql", 9.0),
    ("mysql", 7.0),
    ("forms", 7.0),
    ("sessions", 6.0),
    ("sessions", 7.0),
]


def test_i3302_weights_pool_marks_and_counts_by_recency():
    # 2021 completes by marks (MySQL 4 x 7 = 28 of 88); both 2020 papers fall back to counts,
    # since regex and cookies have no printed mark anywhere (D-67). Votes 3 / 2 / 2.
    weights = topic_weights([PAPER_2021, PAPER_2020_RESIT, PAPER_2020_FIRST])

    assert {topic: round(weight, 1) for topic, weight in weights.items()} == {
        "files": 25.0,
        "mysql": 23.2,
        "sessions": 17.2,
        "forms": 14.4,
        "cookies": 10.7,
        "regex": 9.5,
    }
    assert sum(weights.values()) == pytest.approx(100)


def test_fully_marked_papers_share_by_marks():
    # newest: a 10 of 40 = 25, b 75; older: 50 / 50. Pooled 3 : 2.
    weights = topic_weights([[("a", 10.0), ("b", 30.0)], [("a", 50.0), ("b", 50.0)]])

    assert weights == pytest.approx({"a": 35.0, "b": 65.0})


def test_a_topic_on_one_old_paper_gets_only_that_papers_votes():
    # b is on the third paper only: 100 x 2 of 7 votes; a is 0 there, a real zero.
    weights = topic_weights([[("a", None)], [("a", None)], [("b", None)]])

    assert weights == pytest.approx({"a": 500 / 7, "b": 200 / 7})


def test_a_single_paper_returns_its_own_shares():
    assert topic_weights([[("a", 5.0), ("b", 15.0)]]) == pytest.approx({"a": 25.0, "b": 75.0})


def test_a_paper_whose_marks_are_all_zero_shares_by_count():
    # 0 + 0 is a host writing 0 for "not printed" (D-140): the paper counts its items, a 1 : 1.
    assert topic_weights([[("a", 0.0), ("b", 0.0)]]) == pytest.approx({"a": 50.0, "b": 50.0})


def test_an_all_zero_paper_lends_no_average_to_another_paper():
    # Newest: a unmarked, b 10. Older: a 0, b 0 - unmarked too, so a has no average and the
    # newest paper counts (a 50, b 50). The older counts too: 50 / 50. Pooled 3 : 2.
    weights = topic_weights([[("a", None), ("b", 10.0)], [("a", 0.0), ("b", 0.0)]])

    assert weights == pytest.approx({"a": 50.0, "b": 50.0})


def test_a_zero_average_filling_every_item_shares_by_count():
    # Newest: a 0, b 5 (total 5, a real zero). Older: a unmarked -> average 0, nothing else:
    # its total is 0 after filling, so it counts: a 100. Pooled: a (0 x 3 + 100 x 2) / 5.
    weights = topic_weights([[("a", 0.0), ("b", 5.0)], [("a", None)]])

    assert weights == pytest.approx({"a": 40.0, "b": 60.0})


def test_papers_past_the_third_get_the_older_vote():
    # One topic per paper, votes 3, 2, 2, 1, 1 -> 9 in all.
    papers = [[(name, None)] for name in ("p1", "p2", "p3", "p4", "p5")]

    assert topic_weights(papers) == pytest.approx(
        {"p1": 300 / 9, "p2": 200 / 9, "p3": 200 / 9, "p4": 100 / 9, "p5": 100 / 9}
    )


# --- pin_weights: an instructor's share pins a topic (2.4; D-177) ----------------------------

I3302_V5 = {  # I3302 Final v5's weights, his live profile on 2026-10-16
    "mysql": 39.34,
    "sessions": 16.68,
    "files": 13.53,
    "cookies": 12.66,
    "strings": 6.50,
    "forms": 6.22,
    "regex": 5.07,
}


def test_a_pin_sets_its_topic_and_the_rest_share_what_is_left():
    """ "MySQL is 40%": the Dr's number as said; the others keep their ratio and fill 60."""
    pinned = pin_weights(I3302_V5, {"mysql": 40})

    assert pinned["mysql"] == 40
    assert sum(pinned.values()) == pytest.approx(100)
    assert pinned["sessions"] == pytest.approx(16.68 * 60 / 60.66)
    assert pinned["sessions"] / pinned["files"] == pytest.approx(16.68 / 13.53)


def test_a_zero_pin_takes_a_topic_off_and_the_rest_grow():
    """ "Regex is not on the final"."""
    pinned = pin_weights(I3302_V5, {"regex": 0})

    assert pinned["regex"] == 0
    assert sum(pinned.values()) == pytest.approx(100)
    assert pinned["mysql"] == pytest.approx(39.34 * 100 / 94.93)


def test_a_pinned_topic_no_paper_examined_is_added():
    """A new chapter the Dr says is 20%: it appears, the papers' topics share 80."""
    pinned = pin_weights({"a": 60.0, "b": 40.0}, {"new": 20})

    assert pinned == pytest.approx({"a": 48.0, "b": 32.0, "new": 20})


def test_two_pins_on_different_topics_both_stand():
    pinned = pin_weights({"a": 50.0, "b": 30.0, "c": 20.0}, {"a": 40, "b": 40})

    assert pinned == pytest.approx({"a": 40, "b": 40, "c": 20.0})


def test_pins_summing_to_100_leave_the_others_nothing():
    pinned = pin_weights({"a": 50.0, "b": 30.0, "c": 20.0}, {"a": 70, "b": 30})

    assert pinned == pytest.approx({"a": 70, "b": 30, "c": 0.0})


def test_no_pins_leave_the_weights_as_the_papers_gave_them():
    assert pin_weights(I3302_V5, {}) == pytest.approx(I3302_V5)


def test_unpinned_topics_holding_nothing_split_the_rest_evenly():
    """D-182: the papers gave "b" and "c" 0, so there is no ratio - each takes half of 40."""
    pinned = pin_weights({"a": 100.0, "b": 0.0, "c": 0.0}, {"a": 60})

    assert pinned == pytest.approx({"a": 60, "b": 20.0, "c": 20.0})


def test_every_topic_pinned_keeps_the_pins_as_stated():
    """Nothing left unpinned to share the rest: the Dr's numbers are not stretched to 100."""
    assert pin_weights({"a": 60.0, "b": 40.0}, {"a": 50, "b": 30}) == {"a": 50, "b": 30}


# --- derive_profile: the write unit (D-69) -------------------------------------------------

SEM = "Semester 1 2026-2027"
FINAL = "Final exam"
NAMES = {
    "forms": "PHP forms",
    "files": "PHP files & directories",
    "mysql": "PHP + MySQL (prepared statements)",
    "sessions": "PHP sessions",
    "cookies": "PHP cookies & login/logout",
    "regex": "Regular expressions",
}
I3302_PAPERS = [  # (date, session_type, items), oldest first - the order he registered them
    ("2020-02-17", "first", PAPER_2020_FIRST),
    ("2020-09-14", "second", PAPER_2020_RESIT),
    ("2021-09-20", "second", PAPER_2021),
]


@pytest.fixture
def web(service_engine, user_id):
    return add_course(service_engine, user_id, "I3302", "Server-Side Web Development", SEM)


def register(engine, user_id, tmp_path, date, session_type="first", slot=FINAL) -> str:
    path = write(tmp_path / date, f"I3302_{date}.pdf", pdf_pages([f"paper of {date} {slot}"]))
    return add_past_exam(engine, user_id, "I3302", slot, session_type, date, path)["past_exam_id"]


def transcribe(engine, user_id, paper_id, items) -> None:
    """Transcribe a registered paper: one item per (topic key or name, marks), in order."""
    task_id = start_generation(engine, user_id, "transcription", paper_id).payload["task_id"]
    payload = [
        {
            "position": position,
            "question": f"Question {position}",
            "answer_key": None,
            "marks": marks,
            "topic": {"proposed_name": NAMES.get(key, key)},
        }
        for position, (key, marks) in enumerate(items, start=1)
    ]
    submit_generation(engine, user_id, task_id, {"items": payload})


def add_paper(engine, user_id, tmp_path, date, session_type, items, slot=FINAL) -> str:
    paper_id = register(engine, user_id, tmp_path, date, session_type, slot)
    transcribe(engine, user_id, paper_id, items)
    return paper_id


def accept_all(engine, user_id) -> None:
    with engine.connect() as conn:
        ids = list(conn.execute(select(topic.c.id).where(topic.c.status == "proposed")).scalars())
    for tid in ids:
        confirm_topic_proposal(engine, user_id, tid, "accept")


@pytest.fixture
def i3302(service_engine, user_id, web, tmp_path):
    """I3302's three real papers, transcribed, every topic accepted - the state after 1.12."""
    for date, session_type, items in I3302_PAPERS:
        add_paper(service_engine, user_id, tmp_path, date, session_type, items)
    accept_all(service_engine, user_id)
    return web


def count(engine, table) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def note_task(engine, user_id, course_id, profile_id) -> str:
    """An open note task built on `profile_id` - no note generation exists before 1.14."""
    task_id = new_id()
    with write_unit(engine) as conn:
        conn.execute(
            insert(generation_task).values(
                id=task_id,
                user_id=user_id,
                kind="note",
                scope_type="course",
                scope_id=course_id,
                exam_profile_id=profile_id,
                route="host",
                created_at=now(),
            )
        )
    return task_id


def task_row(engine, task_id):
    with engine.connect() as conn:
        return conn.execute(select(generation_task).where(generation_task.c.id == task_id)).one()


def test_i3302_derives_v1_over_its_active_topics(service_engine, user_id, i3302):
    result = derive_profile(service_engine, user_id, "I3302", FINAL)

    with service_engine.connect() as conn:
        profile = conn.execute(select(exam_profile)).one()
        rows = conn.execute(
            select(topic.c.name, topic.c.status, topic_weight.c.weight).join_from(
                topic_weight, topic, topic_weight.c.topic_id == topic.c.id
            )
        ).all()
    assert (profile.version, profile.slot_id) == (1, i3302["slot_id"])
    assert (profile.provisional_syllabus, profile.provisional_format) == (0, 1)
    assert (profile.evidence_count_syllabus, profile.evidence_count_format) == (24, 0)
    assert {r.name: round(r.weight, 1) for r in rows} == {
        NAMES["files"]: 25.0,
        NAMES["mysql"]: 23.2,
        NAMES["sessions"]: 17.2,
        NAMES["forms"]: 14.4,
        NAMES["cookies"]: 10.7,
        NAMES["regex"]: 9.5,
    }
    assert sum(r.weight for r in rows) == pytest.approx(100)
    assert {r.status for r in rows} == {"active"}
    assert (result["version"], result["papers"], result["invalidated"]) == (1, 3, 0)
    assert [w["name"] for w in result["weights"]][:2] == [NAMES["files"], NAMES["mysql"]]


def test_deriving_again_writes_v2_and_keeps_v1(service_engine, user_id, i3302):
    first = derive_profile(service_engine, user_id, "I3302", FINAL)
    second = derive_profile(service_engine, user_id, "I3302", FINAL)

    with service_engine.connect() as conn:
        v1_weights = conn.execute(
            select(func.count()).where(topic_weight.c.exam_profile_id == first["exam_profile_id"])
        ).scalar_one()
    assert (first["version"], second["version"]) == (1, 2)
    assert count(service_engine, exam_profile) == 2
    assert v1_weights == 6


def test_an_open_task_on_the_older_profile_is_invalidated(service_engine, user_id, i3302):
    v1 = derive_profile(service_engine, user_id, "I3302", FINAL)
    task_id = note_task(service_engine, user_id, i3302["course_id"], v1["exam_profile_id"])

    v2 = derive_profile(service_engine, user_id, "I3302", FINAL)

    task = task_row(service_engine, task_id)
    assert v2["invalidated"] == 1
    assert task.status == "invalidated"
    assert task.closed_at is not None


def test_an_open_task_with_no_profile_is_left_alone(service_engine, user_id, i3302, tmp_path):
    # A fourth paper, registered and its transcription started but not submitted: the task is
    # open with no profile, and the paper is not in the set - it has no items to vote with.
    paper_id = register(service_engine, user_id, tmp_path, "2022-04-13")
    started = start_generation(service_engine, user_id, "transcription", paper_id)
    task_id = started.payload["task_id"]

    result = derive_profile(service_engine, user_id, "I3302", FINAL)

    assert result["papers"] == 3
    assert result["invalidated"] == 0
    assert task_row(service_engine, task_id).status == "open"


# --- a profile bump on a fourth paper (2.5; D-13, D-184) ------------------------------------


def topics(engine) -> dict:
    """Every topic as {id: (name, status)}."""
    with engine.connect() as conn:
        return {
            row.id: (row.name, row.status)
            for row in conn.execute(select(topic.c.id, topic.c.name, topic.c.status))
        }


def topic_id_of(engine, name) -> str:
    with engine.connect() as conn:
        return conn.execute(select(topic.c.id).where(topic.c.name == name)).scalar_one()


def weighed(engine, profile_id) -> set:
    """The topic ids a profile version has a `topic_weight` row for."""
    with engine.connect() as conn:
        return set(
            conn.execute(
                select(topic_weight.c.topic_id).where(topic_weight.c.exam_profile_id == profile_id)
            ).scalars()
        )


def write_review_state(engine, user_id, topic_id):
    """A `topic_state` row as reviews leave it - written directly, as no derive writes one."""
    with write_unit(engine) as conn:
        conn.execute(
            insert(topic_state).values(
                user_id=user_id,
                topic_id=topic_id,
                stability=3.2,
                difficulty=5.1,
                due_at=now(),
                last_reviewed_at=now(),
                reps=3,
                lapses=1,
                inferred=0,
                updated_at=now(),
            )
        )
    return state_row(engine, topic_id)


def state_row(engine, topic_id):
    with engine.connect() as conn:
        return conn.execute(select(topic_state).where(topic_state.c.topic_id == topic_id)).one()


def test_a_fourth_paper_with_a_new_topic_bumps_weighs_it_and_invalidates(
    service_engine, user_id, i3302, tmp_path
):
    """The transcription adds AJAX, he confirms it, the bump weighs it (D-184). The old topics,
    the MySQL review schedule and v1 stay as they were; the task built on v1 is invalidated."""
    v1 = derive_profile(service_engine, user_id, "I3302", FINAL)
    task_id = note_task(service_engine, user_id, i3302["course_id"], v1["exam_profile_id"])
    state = write_review_state(service_engine, user_id, topic_id_of(service_engine, NAMES["mysql"]))
    before = topics(service_engine)
    add_paper(
        service_engine, user_id, tmp_path, "2022-04-13", "first", [("forms", 10.0), ("AJAX", 10.0)]
    )
    accept_all(service_engine, user_id)

    v2 = derive_profile(service_engine, user_id, "I3302", FINAL)

    ajax = topic_id_of(service_engine, "AJAX")
    assert (v2["version"], v2["papers"], v2["invalidated"]) == (2, 4, 1)
    assert weighed(service_engine, v2["exam_profile_id"]) == set(before) | {ajax}
    assert weighed(service_engine, v1["exam_profile_id"]) == set(before)
    after = topics(service_engine)
    assert {tid: after[tid] for tid in before} == before  # no rename, no retire
    assert state_row(service_engine, state.topic_id) == state
    assert task_row(service_engine, task_id).status == "invalidated"


def test_a_derive_refused_on_a_proposed_topic_invalidates_nothing(
    service_engine, user_id, i3302, tmp_path
):
    """AJAX still proposed: derive refuses before it writes, so the task built on v1 stays open."""
    v1 = derive_profile(service_engine, user_id, "I3302", FINAL)
    task_id = note_task(service_engine, user_id, i3302["course_id"], v1["exam_profile_id"])
    add_paper(service_engine, user_id, tmp_path, "2022-04-13", "first", [("AJAX", 10.0)])

    with pytest.raises(StudyError) as err:
        derive_profile(service_engine, user_id, "I3302", FINAL)

    assert err.value.code == "topics_unconfirmed"
    assert count(service_engine, exam_profile) == 1
    assert task_row(service_engine, task_id).status == "open"


def test_two_transcribed_papers_are_too_few(service_engine, user_id, web, tmp_path):
    for date, session_type, items in I3302_PAPERS[:2]:
        add_paper(service_engine, user_id, tmp_path, date, session_type, items)
    accept_all(service_engine, user_id)

    with pytest.raises(StudyError) as err:
        derive_profile(service_engine, user_id, "I3302", FINAL)

    assert err.value.code == "too_few_papers"
    assert count(service_engine, exam_profile) == 0


def test_an_untranscribed_paper_does_not_count(service_engine, user_id, web, tmp_path):
    for date, session_type, items in I3302_PAPERS[:2]:
        add_paper(service_engine, user_id, tmp_path, date, session_type, items)
    register(service_engine, user_id, tmp_path, "2021-09-20", "second")  # never transcribed
    accept_all(service_engine, user_id)

    with pytest.raises(StudyError) as err:
        derive_profile(service_engine, user_id, "I3302", FINAL)

    assert err.value.code == "too_few_papers"


def test_an_item_on_a_proposed_topic_refuses_and_writes_nothing(
    service_engine, user_id, web, tmp_path
):
    for date, session_type, items in I3302_PAPERS:
        add_paper(service_engine, user_id, tmp_path, date, session_type, items)
    # every topic still proposed - none accepted yet

    with pytest.raises(StudyError) as err:
        derive_profile(service_engine, user_id, "I3302", FINAL)

    assert err.value.code == "topics_unconfirmed"
    assert NAMES["forms"] in err.value.message  # the newest paper's first item, by name
    assert count(service_engine, exam_profile) == 0
    assert count(service_engine, topic_weight) == 0


def test_another_slots_papers_are_ignored(service_engine, user_id, i3302, tmp_path):
    add_assessment(service_engine, user_id, "I3302", "Midterm", "exam")
    midterm_items = [("Midterm only", 10.0)]
    add_paper(service_engine, user_id, tmp_path, "2022-11-10", "first", midterm_items, "Midterm")
    # left proposed on purpose: the Final's guard reads only the Final's papers

    result = derive_profile(service_engine, user_id, "I3302", FINAL)

    assert result["papers"] == 3
    assert "Midterm only" not in {w["name"] for w in result["weights"]}


# --- rejected items are not evidence --------------------------------------------------------


def set_state(engine, paper_id, positions, state) -> None:
    with write_unit(engine) as conn:
        conn.execute(
            update(practice_item)
            .where(
                practice_item.c.past_exam_id == paper_id,
                practice_item.c.position.in_(positions),
            )
            .values(state=state)
        )


def paper_on(engine, date) -> str:
    with engine.connect() as conn:
        return conn.execute(
            select(past_exam.c.id).where(past_exam.c.session_date == date)
        ).scalar_one()


def by_name(result) -> dict:
    return {w["name"]: w["weight"] for w in result["weights"]}


def test_a_rejected_item_is_left_out_of_the_weights(service_engine, user_id, i3302):
    # 2021's forms question (23 marks) rejected: the paper weighs as if it never asked it.
    set_state(service_engine, paper_on(service_engine, "2021-09-20"), [1], "rejected")

    result = derive_profile(service_engine, user_id, "I3302", FINAL)

    expected = topic_weights([PAPER_2021[1:], PAPER_2020_RESIT, PAPER_2020_FIRST])
    assert by_name(result) == pytest.approx({NAMES[k]: w for k, w in expected.items()})
    assert result["papers"] == 3
    with service_engine.connect() as conn:
        evidence = conn.execute(select(exam_profile.c.evidence_count_syllabus)).scalar_one()
    assert evidence == 23  # 24 items, one rejected


def test_a_paper_with_every_item_rejected_does_not_count_toward_the_minimum(
    service_engine, user_id, i3302
):
    set_state(service_engine, paper_on(service_engine, "2021-09-20"), range(1, 8), "rejected")

    with pytest.raises(StudyError) as err:
        derive_profile(service_engine, user_id, "I3302", FINAL)

    assert err.value.code == "too_few_papers"
    assert count(service_engine, exam_profile) == 0


def test_a_rejected_item_on_a_proposed_topic_does_not_block(
    service_engine, user_id, i3302, tmp_path
):
    # I3306's case: a project question on its own topic, left proposed and rejected.
    paper_id = add_paper(
        service_engine,
        user_id,
        tmp_path,
        "2022-04-13",
        "first",
        [("forms", 10.0), ("Project", None)],
    )
    set_state(service_engine, paper_id, [2], "rejected")

    result = derive_profile(service_engine, user_id, "I3302", FINAL)

    assert result["papers"] == 4
    assert "Project" not in by_name(result)


def test_a_rejected_item_set_live_again_counts_again(service_engine, user_id, i3302):
    before = by_name(derive_profile(service_engine, user_id, "I3302", FINAL))
    paper_id = paper_on(service_engine, "2021-09-20")
    set_state(service_engine, paper_id, [1], "rejected")
    rejected = by_name(derive_profile(service_engine, user_id, "I3302", FINAL))
    set_state(service_engine, paper_id, [1], "live")

    after = by_name(derive_profile(service_engine, user_id, "I3302", FINAL))

    assert rejected != pytest.approx(before)
    assert after == pytest.approx(before)


def test_derive_through_the_tool(call, service_engine, user_id, i3302):
    result = call("study_derive_profile", {"code": "I3302", "assessment": FINAL})

    assert not result.is_error
    body = json.loads(result.content[0].text)
    weights = [w["weight"] for w in body["weights"]]
    assert body["version"] == 1
    assert weights == sorted(weights, reverse=True)
    assert body["weights"][0]["name"] == NAMES["files"]
    assert body["provisional"]["syllabus"] == {"provisional": False, "why": "3 papers; 3 needed"}


# --- another student (Conventions: ownership) ---------------------------------


def test_another_students_papers_never_count_toward_a_profile(
    service_engine, user_id, other_user_id, i3302
):
    """A has three transcribed papers; B's own I3302 Final has none - too few for B."""
    add_course(service_engine, other_user_id, "I3302", "Server-Side Web Development", SEM)

    with pytest.raises(StudyError) as err:
        derive_profile(service_engine, other_user_id, "I3302", FINAL)

    assert err.value.code == "too_few_papers"
    assert count(service_engine, exam_profile) == 0
    assert count(service_engine, topic_weight) == 0


# --- claims routed by class (2.1b; D-150, D-156 - D-159) ----------------------------------------

DUR, COUNT, TRAP = "duration-minutes", "question-count", "trap"


def observe(engine, user_id, paper_id, key, value) -> str:
    """One observation on a paper, as an observation task's submit stores it (E1, D-149)."""
    obs_id = new_id()
    claim_class = "format" if key in (DUR, COUNT) else "instructor-driven"
    with write_unit(engine) as conn:
        slot_id = conn.execute(
            select(past_exam.c.slot_id).where(past_exam.c.id == paper_id)
        ).scalar_one()
        conn.execute(
            insert(observation).values(
                id=obs_id,
                owner_id=user_id,
                slot_id=slot_id,
                past_exam_id=paper_id,
                claim_key=key,
                claim_class=claim_class,
                claim_value=json.dumps(value),
                study_tier="E1",
                created_at=now(),
            )
        )
    return obs_id


def name_instructor(engine, paper_id, name) -> None:
    with write_unit(engine) as conn:
        conn.execute(
            update(past_exam)
            .where(past_exam.c.id == paper_id)
            .values(instructor=name, instructor_tier="declared")
        )


def kept_by(engine, profile_id) -> set[str]:
    with engine.connect() as conn:
        return set(
            conn.execute(
                select(profile_claim.c.observation_id).where(
                    profile_claim.c.exam_profile_id == profile_id
                )
            ).scalars()
        )


@pytest.fixture
def claims(service_engine, user_id, i3302):
    """The three papers with an instructor and claims each - names stand-ins, his live 2021-09
    is Hamze's: 2021-09 Ghorayeb (90 min, a trap), 2020-09 Hamze (120 min, a trap), 2020-02
    unnamed (5 questions). The course: "A. Ghrayeb"."""
    newest, resit, first = (
        paper_on(service_engine, d) for d in ("2021-09-20", "2020-09-14", "2020-02-17")
    )
    name_instructor(service_engine, newest, "Dr. Ali Ghorayeb")
    name_instructor(service_engine, resit, "Dr. Mohamad Hamze")
    set_course_input(service_engine, user_id, "I3302", "instructor", "A. Ghrayeb")
    return {
        "dur_90": observe(service_engine, user_id, newest, DUR, {"minutes": 90}),
        "ghorayeb_trap": observe(service_engine, user_id, newest, TRAP, {"text": "== vs ==="}),
        "dur_120": observe(service_engine, user_id, resit, DUR, {"minutes": 120}),
        "hamze_trap": observe(service_engine, user_id, resit, TRAP, {"text": "$_GET in a query"}),
        "count_5": observe(service_engine, user_id, first, COUNT, {"count": 5}),
    }


def test_a_profile_keeps_each_class_by_its_own_rule(service_engine, user_id, claims):
    """Format from one paper only - here the 2021-09 resit, its first session being past the cap
    (D-169); instructor claims from papers by who teaches now."""
    result = derive_profile(service_engine, user_id, "I3302", FINAL, this_year=2026)

    with service_engine.connect() as conn:
        profile = conn.execute(select(exam_profile)).one()
    assert kept_by(service_engine, profile.id) == {claims["dur_90"], claims["ghorayeb_trap"]}
    assert (profile.evidence_count_instructor, profile.evidence_count_format) == (1, 1)
    assert (profile.provisional_instructor, profile.provisional_format) == (1, 1)  # 2.2
    assert result["claims"] == {"format": 1, "instructor-driven": 1}


def test_an_instructor_change_excludes_the_old_instructors_claims(service_engine, user_id, claims):
    """2.1's Done when, on the database: Hamze declared as teaching now, the next version keeps
    his trap and drops Ghorayeb's; format does not move, and v1 still says what it kept."""
    v1 = derive_profile(service_engine, user_id, "I3302", FINAL)
    set_course_input(service_engine, user_id, "I3302", "instructor", "M. Hamze")
    v2 = derive_profile(service_engine, user_id, "I3302", FINAL)

    assert kept_by(service_engine, v2["exam_profile_id"]) == {
        claims["dur_90"],
        claims["hamze_trap"],
    }
    assert kept_by(service_engine, v1["exam_profile_id"]) == {
        claims["dur_90"],
        claims["ghorayeb_trap"],
    }


def test_a_paper_outside_the_weights_set_lends_no_claim(service_engine, user_id, claims, tmp_path):
    """A newer paper with observations but no transcription sits out until transcribed (D-159):
    its 75 minutes do not displace the newest transcribed paper's 90."""
    untranscribed = register(service_engine, user_id, tmp_path, "2023-09-13", "second")
    name_instructor(service_engine, untranscribed, "Dr. Ali Ghorayeb")
    observe(service_engine, user_id, untranscribed, DUR, {"minutes": 75})

    result = derive_profile(service_engine, user_id, "I3302", FINAL)

    assert kept_by(service_engine, result["exam_profile_id"]) == {
        claims["dur_90"],
        claims["ghorayeb_trap"],
    }
    assert result["papers"] == 3


def test_a_course_with_no_instructor_keeps_format_only(service_engine, user_id, i3302):
    """No one declared as teaching now: no paper is by them (D-150)."""
    paper = paper_on(service_engine, "2021-09-20")
    name_instructor(service_engine, paper, "Dr. Ali Ghorayeb")
    dur = observe(service_engine, user_id, paper, DUR, {"minutes": 90})
    observe(service_engine, user_id, paper, TRAP, {"text": "== vs ==="})

    result = derive_profile(service_engine, user_id, "I3302", FINAL)

    assert kept_by(service_engine, result["exam_profile_id"]) == {dur}
    assert result["claims"] == {"format": 1, "instructor-driven": 0}


def test_a_profile_links_only_its_owners_observations_of_its_own_slot(
    service_engine, user_id, other_user_id, claims, tmp_path
):
    """physical-schema "Ownership" and "Same course": a link's observation has the profile's
    owner and slot. Three rows that must never be linked - another student's observation on
    this paper, an observation on another slot's paper, and one whose slot is not its paper's."""
    newest = paper_on(service_engine, "2021-09-20")
    foreign = observe(service_engine, other_user_id, newest, TRAP, {"text": "not his"})
    add_assessment(service_engine, user_id, "I3302", "Midterm", "exam")
    midterm = add_paper(
        service_engine, user_id, tmp_path, "2022-11-10", "first", [("forms", 10.0)], "Midterm"
    )
    accept_all(service_engine, user_id)
    on_midterm = observe(service_engine, user_id, midterm, TRAP, {"text": "midterm trap"})
    stray = observe(service_engine, user_id, newest, TRAP, {"text": "filed on the midterm"})
    with write_unit(service_engine) as conn:
        midterm_slot = conn.execute(
            select(past_exam.c.slot_id).where(past_exam.c.id == midterm)
        ).scalar_one()
        conn.execute(
            update(observation).where(observation.c.id == stray).values(slot_id=midterm_slot)
        )

    result = derive_profile(service_engine, user_id, "I3302", FINAL)

    kept = kept_by(service_engine, result["exam_profile_id"])
    assert kept == {claims["dur_90"], claims["ghorayeb_trap"]}
    assert not kept & {foreign, on_midterm, stray}


# --- provisional per class, and the reply says why (2.2; D-164 - D-167) ------------------------


def flags(engine, profile_id) -> tuple[int, int, int]:
    with engine.connect() as conn:
        row = conn.execute(select(exam_profile).where(exam_profile.c.id == profile_id)).one()
    return (row.provisional_syllabus, row.provisional_instructor, row.provisional_format)


def test_a_profile_states_each_class_provisional_and_why(service_engine, user_id, claims):
    """(g) 2.2's Done when on the fixture: one Ghorayeb paper of three is below the instructor
    bar; syllabus is cleared by the refusal (D-164); format comes from a resit, so it is
    provisional and says why (D-169, D-170)."""
    result = derive_profile(service_engine, user_id, "I3302", FINAL, this_year=2026)

    assert flags(service_engine, result["exam_profile_id"]) == (0, 1, 1)
    assert result["provisional"] == {
        "syllabus": {"provisional": False, "why": "3 papers; 3 needed"},
        "instructor-driven": {
            "provisional": True,
            "why": "1 of 3 papers by A. Ghrayeb; 2 needed - 2020-02-17 names no instructor, "
            "assumed by its nearest named paper's",
        },
        "format": {
            "provisional": True,
            "why": "format from the 2021-09-20 second-session paper: the newest first-session "
            "paper, 2020-02-17, is more than 5 years old",
        },
    }


def test_the_instructor_flag_follows_who_teaches_now(service_engine, user_id, claims):
    """(h) Hamze declared as teaching now, and he set two of the three papers: the next version
    clears the instructor flag; v1, derived under Ghrayeb, still reads provisional."""
    name_instructor(service_engine, paper_on(service_engine, "2020-02-17"), "Dr. Mohamad Hamze")
    v1 = derive_profile(service_engine, user_id, "I3302", FINAL)
    set_course_input(service_engine, user_id, "I3302", "instructor", "M. Hamze")

    v2 = derive_profile(service_engine, user_id, "I3302", FINAL)

    assert flags(service_engine, v1["exam_profile_id"])[1] == 1
    assert flags(service_engine, v2["exam_profile_id"])[1] == 0
    assert v2["provisional"]["instructor-driven"] == {
        "provisional": False,
        "why": "2 of 3 papers by M. Hamze; 2 needed",
    }


# --- a paper naming no instructor counts as its nearest named one's (2.4; D-174 - D-176) --------


def assumed_count(engine, profile_id) -> int:
    with engine.connect() as conn:
        return conn.execute(
            select(exam_profile.c.assumed_instructor_papers).where(exam_profile.c.id == profile_id)
        ).scalar_one()


def test_a_profile_stores_how_many_papers_it_assumed(service_engine, user_id, claims):
    """2020-02 names no one: it is assumed Hamze's (the newer 2020-09), and the row says 1
    (D-176) - so the dashboard sees the assumption without the reply."""
    result = derive_profile(service_engine, user_id, "I3302", FINAL)

    assert assumed_count(service_engine, result["exam_profile_id"]) == 1


def test_an_unnamed_papers_traps_count_as_its_neighbours(service_engine, user_id, claims):
    """Hamze declared as teaching now: 2020-02 names no one but sits after his 2020-09, so its
    trap is kept as his, and it counts toward his bar (D-174, D-175); Ghorayeb's is not."""
    first_trap = observe(
        service_engine, user_id, paper_on(service_engine, "2020-02-17"), TRAP, {"text": "isset"}
    )
    set_course_input(service_engine, user_id, "I3302", "instructor", "M. Hamze")

    result = derive_profile(service_engine, user_id, "I3302", FINAL)

    kept = kept_by(service_engine, result["exam_profile_id"])
    assert {claims["hamze_trap"], first_trap} <= kept
    assert claims["ghorayeb_trap"] not in kept
    assert result["provisional"]["instructor-driven"] == {
        "provisional": False,
        "why": "2 of 3 papers by M. Hamze; 2 needed - 2020-02-17 names no instructor, assumed "
        "by its nearest named paper's",
    }


def test_a_profile_derived_with_every_paper_named_assumes_none(service_engine, user_id, claims):
    name_instructor(service_engine, paper_on(service_engine, "2020-02-17"), "Dr. Mohamad Hamze")

    result = derive_profile(service_engine, user_id, "I3302", FINAL)

    assert assumed_count(service_engine, result["exam_profile_id"]) == 0


def test_a_month_only_paper_prints_its_month_in_both_reasons(service_engine, user_id, claims):
    """2020-02-17 marked month-only (stored as the 1st, D-137): both reasons print "2020-02",
    never a day the paper does not print (D-181)."""
    first = paper_on(service_engine, "2020-02-17")  # read before the write unit takes the lock
    with write_unit(service_engine) as conn:
        conn.execute(
            update(past_exam)
            .where(past_exam.c.id == first)
            .values(session_date="2020-02-01", session_date_approx=1)
        )

    result = derive_profile(service_engine, user_id, "I3302", FINAL, this_year=2026)

    assert result["provisional"]["instructor-driven"]["why"] == (
        "1 of 3 papers by A. Ghrayeb; 2 needed - 2020-02 names no instructor, assumed by its "
        "nearest named paper's"
    )
    assert result["provisional"]["format"]["why"] == (
        "format from the 2021-09-20 second-session paper: the newest first-session paper, "
        "2020-02, is more than 5 years old"
    )


# --- format by the session he sits next (2.3; D-168 - D-170) ---------------------------------


def sit(engine, user_id, session_type) -> None:
    """Set the Final's sitting to `session_type` - the session he sits next (D-168)."""
    with write_unit(engine) as conn:
        conn.execute(
            update(assessment)
            .where(assessment.c.user_id == user_id, assessment.c.session_type.is_not(None))
            .values(session_type=session_type)
        )


def test_the_first_session_sets_format_over_a_newer_resit(service_engine, user_id, claims):
    """2.3's Done when, on the fixture: seen from 2022, the 2020-02 first session is within the
    cap, so its 5 questions are kept and the newer resits' minutes are not."""
    result = derive_profile(service_engine, user_id, "I3302", FINAL, this_year=2022)

    assert kept_by(service_engine, result["exam_profile_id"]) == {
        claims["count_5"],
        claims["ghorayeb_trap"],
    }
    assert result["provisional"]["format"] == {
        "provisional": True,
        "why": "format from 2020-02-17; 1 of 1 first-session papers since 2017 read; 2 needed",
    }


def test_a_student_sitting_the_resit_matches_the_resits(service_engine, user_id, claims):
    """The sitting reads `second`: format from the newest resit, and two resits read clear it."""
    sit(service_engine, user_id, "second")

    result = derive_profile(service_engine, user_id, "I3302", FINAL, this_year=2022)

    assert kept_by(service_engine, result["exam_profile_id"]) == {
        claims["dur_90"],
        claims["ghorayeb_trap"],
    }
    assert result["provisional"]["format"] == {
        "provisional": False,
        "why": "format from 2021-09-20; 2 of 2 second-session papers since 2017 read; 2 needed",
    }
    assert flags(service_engine, result["exam_profile_id"])[2] == 0


def test_a_slot_with_a_resit_sitting_too_matches_the_first(service_engine, user_id, claims):
    """Two sittings on the Final, first and second (unique per slot): `first` (D-168)."""
    with write_unit(service_engine) as conn:
        slot_id = conn.execute(
            select(past_exam.c.slot_id).where(past_exam.c.session_date == "2020-02-17")
        ).scalar_one()
        conn.execute(
            insert(assessment).values(
                id=new_id(),
                user_id=user_id,
                slot_id=slot_id,
                weight_tier="unknown",
                session_type="second",
                created_at=now(),
            )
        )

    result = derive_profile(service_engine, user_id, "I3302", FINAL, this_year=2022)

    assert claims["count_5"] in kept_by(service_engine, result["exam_profile_id"])


# --- instructor statements (2.4; D-172, D-173, D-177 - D-180) --------------------------------


def topic_named(engine, key) -> str:
    with engine.connect() as conn:
        return conn.execute(select(topic.c.id).where(topic.c.name == NAMES[key])).scalar_one()


def state(engine, user_id, key, value, topic=None, **extra) -> dict:
    """The student reports one thing the instructor said about I3302's Final."""
    return add_instructor_statement(engine, user_id, "I3302", FINAL, key, value, topic, **extra)


def entered_on(engine, obs_id, at) -> None:
    """Move a statement's `created_at` back, as if entered on `at` (D-179)."""
    with write_unit(engine) as conn:
        conn.execute(update(observation).where(observation.c.id == obs_id).values(created_at=at))


def test_a_statement_is_stored_as_e2_with_no_paper(service_engine, user_id, i3302):
    """D-173: one observation, E2, no paper, class fixed by key; its evidence row E2; the reply
    repeats it."""
    reply = state(service_engine, user_id, DUR, {"minutes": 120}, citation="lecture 2026-10-16")

    with service_engine.connect() as conn:
        row = conn.execute(select(observation)).one()
        ev = conn.execute(select(evidence).where(evidence.c.observation_id == row.id)).one()
    assert (row.study_tier, row.past_exam_id, row.claim_class) == ("E2", None, "format")
    assert json.loads(row.claim_value) == {"minutes": 120}
    assert (ev.study_tier, ev.locator) == ("E2", "lecture 2026-10-16")
    assert reply["observation_id"] == row.id
    assert reply["topic"] is None
    assert reply["stated"] == (
        "I3302 Final exam, from the instructor: the exam lasts 120 minutes. Derive the profile "
        "again to use it."
    )


def test_a_topic_share_names_its_topic(service_engine, user_id, i3302):
    mysql = topic_named(service_engine, "mysql")

    reply = state(service_engine, user_id, "topic-marks", {"marks": 40, "of_total": 100}, mysql)

    assert reply["topic"] == {"id": mysql, "name": NAMES["mysql"]}
    with service_engine.connect() as conn:
        assert conn.execute(select(observation.c.topic_id)).scalar_one() == mysql


@pytest.mark.parametrize(
    ("key", "value", "topic_key", "field"),
    [
        (TRAP, {"text": "== vs ==="}, None, "claim_key"),  # instructor-driven: not a statement
        ("pages", {"count": 3}, None, "claim_key"),  # no such key
        (DUR, {"minutes": "two hours"}, None, "claim_value"),  # wrong shape
        ("topic-marks", {"marks": 40}, "mysql", "claim_value"),  # no total
        ("topic-marks", {"marks": 120, "of_total": 100}, "mysql", "claim_value"),  # past it
        ("topic-marks", {"marks": 40, "of_total": 100}, None, "topic_id"),  # no topic
        (DUR, {"minutes": 120}, "mysql", "topic_id"),  # a format fact names no topic
    ],
)
def test_a_bad_statement_is_refused_and_writes_nothing(
    service_engine, user_id, i3302, key, value, topic_key, field
):
    topic_id = topic_named(service_engine, topic_key) if topic_key else None

    with pytest.raises(StudyError) as err:
        state(service_engine, user_id, key, value, topic_id)

    assert err.value.code == "invalid_value"
    assert err.value.field_errors[0]["field"] == field
    assert count(service_engine, observation) == 0


def test_a_topic_not_confirmed_in_this_course_is_not_found(service_engine, user_id, i3302):
    with pytest.raises(StudyError) as err:
        state(service_engine, user_id, "topic-marks", {"marks": 40, "of_total": 100}, "9" * 26)

    assert err.value.code == "not_found"
    assert count(service_engine, observation) == 0


def test_shares_past_100_are_refused(service_engine, user_id, i3302):
    """D-180: MySQL 60 stands; sessions 50 would make 110."""
    mysql, sessions = topic_named(service_engine, "mysql"), topic_named(service_engine, "sessions")
    state(service_engine, user_id, "topic-marks", {"marks": 60, "of_total": 100}, mysql)

    with pytest.raises(StudyError) as err:
        state(service_engine, user_id, "topic-marks", {"marks": 50, "of_total": 100}, sessions)

    assert err.value.code == "pins_over_100"
    assert count(service_engine, observation) == 1


def test_a_correction_replaces_the_older_share_in_the_sum(service_engine, user_id, i3302):
    """MySQL 60 corrected to 30: the newer replaces the older (D-177), so sessions 50 fits."""
    mysql, sessions = topic_named(service_engine, "mysql"), topic_named(service_engine, "sessions")
    first = state(service_engine, user_id, "topic-marks", {"marks": 60, "of_total": 100}, mysql)
    entered_on(service_engine, first["observation_id"], "2022-01-10T09:00:00Z")  # after 2021-09
    state(service_engine, user_id, "topic-marks", {"marks": 30, "of_total": 100}, mysql)

    state(service_engine, user_id, "topic-marks", {"marks": 50, "of_total": 100}, sessions)

    assert count(service_engine, observation) == 3


def test_a_retired_share_does_not_count_toward_100(service_engine, user_id, i3302):
    """MySQL 60 entered before the newest paper (2021-09-20) is retired (D-179)."""
    mysql, sessions = topic_named(service_engine, "mysql"), topic_named(service_engine, "sessions")
    old = state(service_engine, user_id, "topic-marks", {"marks": 60, "of_total": 100}, mysql)
    entered_on(service_engine, old["observation_id"], "2021-01-10T09:00:00Z")

    state(service_engine, user_id, "topic-marks", {"marks": 50, "of_total": 100}, sessions)

    assert count(service_engine, observation) == 2


def test_a_stated_duration_outranks_the_papers(service_engine, user_id, claims):
    """2.4's Done when, on the fixture: format comes from the 2021-09 resit (90 min); "the final
    is 2 hours" replaces its duration, keeps its other keys' rule, and clears the flag (D-172,
    D-178)."""
    stated = state(service_engine, user_id, DUR, {"minutes": 120})

    result = derive_profile(service_engine, user_id, "I3302", FINAL, this_year=2026)

    kept = kept_by(service_engine, result["exam_profile_id"])
    assert stated["observation_id"] in kept
    assert claims["dur_90"] not in kept
    assert result["statements"] == [
        {"claim_key": DUR, "claim_value": {"minutes": 120}, "topic_id": None}
    ]
    assert result["claims"]["format"] == 1
    assert result["provisional"]["format"] == {
        "provisional": False,
        "why": "duration-minutes from the instructor; format from the 2021-09-20 second-session "
        "paper: the newest first-session paper, 2020-02-17, is more than 5 years old",
    }
    assert flags(service_engine, result["exam_profile_id"])[2] == 0


def test_a_stated_share_pins_its_topic(service_engine, user_id, i3302):
    """ "MySQL is 40%": its weight is 40, marked as the instructor's; the rest fill 60 (D-177)."""
    mysql = topic_named(service_engine, "mysql")
    before = by_name(derive_profile(service_engine, user_id, "I3302", FINAL))
    state(service_engine, user_id, "topic-marks", {"marks": 40, "of_total": 100}, mysql)

    result = derive_profile(service_engine, user_id, "I3302", FINAL)

    after = by_name(result)
    assert after[NAMES["mysql"]] == pytest.approx(40)
    assert sum(after.values()) == pytest.approx(100)
    rest = 100 - before[NAMES["mysql"]]
    assert after[NAMES["files"]] == pytest.approx(before[NAMES["files"]] * 60 / rest)
    sources = {w["name"]: w["source"] for w in result["weights"]}
    assert sources[NAMES["mysql"]] == "instructor"
    assert sources[NAMES["files"]] == "papers"
    with service_engine.connect() as conn:
        stored = conn.execute(
            select(topic_weight.c.weight).where(
                topic_weight.c.exam_profile_id == result["exam_profile_id"],
                topic_weight.c.topic_id == mysql,
            )
        ).scalar_one()
    assert stored == pytest.approx(40)


def test_a_paper_dated_after_a_statement_retires_it(service_engine, user_id, claims):
    """Entered before the newest paper (2021-09-20): that exam has come and gone (D-179)."""
    stated = state(service_engine, user_id, DUR, {"minutes": 120})
    entered_on(service_engine, stated["observation_id"], "2021-01-10T09:00:00Z")

    result = derive_profile(service_engine, user_id, "I3302", FINAL, this_year=2026)

    assert result["statements"] == []
    assert claims["dur_90"] in kept_by(service_engine, result["exam_profile_id"])
    assert result["provisional"]["format"]["provisional"] is True


def test_a_share_is_repeated_in_words(service_engine, user_id, i3302):
    mysql = topic_named(service_engine, "mysql")

    reply = state(service_engine, user_id, "topic-marks", {"marks": 0, "of_total": 100}, mysql)

    assert reply["stated"].startswith(
        f"I3302 Final exam, from the instructor: {NAMES['mysql']} is not on the exam."
    )


def test_a_statement_through_the_tool(call, service_engine, user_id, i3302):
    result = call(
        "study_add_instructor_statement",
        {"code": "I3302", "assessment": FINAL, "claim_key": DUR, "claim_value": {"minutes": 120}},
    )

    assert not result.is_error
    body = json.loads(result.content[0].text)
    assert body["claim_key"] == DUR
    assert count(service_engine, observation) == 1


def test_a_bad_value_through_the_tool_is_structured(call, service_engine, user_id, i3302):
    """A string where an object belongs reaches the service's check, not the SDK's (D-36)."""
    result = call(
        "study_add_instructor_statement",
        {"code": "I3302", "assessment": FINAL, "claim_key": DUR, "claim_value": "two hours"},
    )

    assert result.is_error
    assert json.loads(result.content[0].text)["code"] == "invalid_value"


def share(obs_id, topic_id, marks, at) -> dict:
    """One `topic-marks` statement as `counting_statements` gives it."""
    return {
        "id": obs_id,
        "claim_key": "topic-marks",
        "claim_class": "syllabus-driven",
        "topic_id": topic_id,
        "claim_value": {"marks": marks, "of_total": 100},
        "created_at": at,
    }


def test_shares_within_100_are_all_used():
    statements = [share("a", "mysql", 40, "2026-10-16T09:00:00Z"), share("b", "regex", 0, AT2)]
    assert fit_statements(statements) == (statements, [])


AT2 = "2026-10-20T09:00:00Z"


def test_shares_past_100_set_the_oldest_aside():
    """D-183: the older 60 no longer fits beside the newer 50; the format one always stays."""
    old = share("old", "mysql", 60, "2022-01-10T09:00:00Z")
    new = share("new", "sessions", 50, AT2)
    duration = {
        "id": "dur",
        "claim_key": DUR,
        "claim_class": "format",
        "topic_id": None,
        "claim_value": {"minutes": 120},
        "created_at": "2021-01-01T00:00:00Z",
    }

    assert fit_statements([old, new, duration]) == ([new, duration], [old])


def test_thirds_fit_despite_float_error():
    thirds = [share(str(i), f"t{i}", 100 / 3, f"2026-10-1{i}T09:00:00Z") for i in range(3)]
    assert fit_statements(thirds) == (thirds, [])


def test_a_revived_share_gives_way_at_derive(service_engine, user_id, i3302):
    """QA's case: MySQL 60% was retired when sessions 50% went in; the paper that retired it
    leaves the set (here: its date moved after the newest paper, 2021-09-20), so it counts
    again. Derive keeps the newer sessions 50, sets MySQL aside, and says so (D-183)."""
    mysql, sessions = topic_named(service_engine, "mysql"), topic_named(service_engine, "sessions")
    old = state(service_engine, user_id, "topic-marks", {"marks": 60, "of_total": 100}, mysql)
    entered_on(service_engine, old["observation_id"], "2021-01-10T09:00:00Z")  # retired
    state(service_engine, user_id, "topic-marks", {"marks": 50, "of_total": 100}, sessions)
    entered_on(service_engine, old["observation_id"], "2022-01-10T09:00:00Z")  # revived

    result = derive_profile(service_engine, user_id, "I3302", FINAL)

    weights = by_name(result)
    assert weights[NAMES["sessions"]] == pytest.approx(50)
    assert min(weights.values()) >= 0
    assert sum(weights.values()) == pytest.approx(100)
    assert result["statements_set_aside"] == [
        {
            "claim_key": "topic-marks",
            "claim_value": {"marks": 60, "of_total": 100},
            "topic_id": mysql,
        }
    ]
    assert old["observation_id"] not in kept_by(service_engine, result["exam_profile_id"])


def test_shares_a_hair_past_100_in_floats_leave_the_rest_at_0():
    """115/120 + 5/120 is 100% exactly, 100.00000000000001 in floats: the others get 0, never
    a tiny negative (QA, 2026-10-16)."""
    pinned = pin_weights(
        {"a": 50.0, "b": 30.0, "c": 20.0}, {"a": 115 / 120 * 100, "b": 5 / 120 * 100}
    )

    assert pinned["c"] == 0
    assert min(pinned.values()) >= 0


def test_shares_making_exactly_100_derive(service_engine, user_id, i3302):
    """The same pair through the tool and derive: both stored, the profile written, every other
    topic at 0 - before the fix, the weight CHECK refused the insert."""
    mysql, regex = topic_named(service_engine, "mysql"), topic_named(service_engine, "regex")
    state(service_engine, user_id, "topic-marks", {"marks": 115, "of_total": 120}, mysql)
    state(service_engine, user_id, "topic-marks", {"marks": 5, "of_total": 120}, regex)

    result = derive_profile(service_engine, user_id, "I3302", FINAL)

    weights = by_name(result)
    assert weights[NAMES["mysql"]] == pytest.approx(115 / 120 * 100)
    assert weights[NAMES["regex"]] == pytest.approx(5 / 120 * 100)
    assert min(weights.values()) >= 0
    assert sum(weights.values()) == pytest.approx(100)


def test_a_sole_unpinned_topic_never_passes_100():
    """Every other topic "not on the exam": the last one takes all 100 - in floats
    0.0244... x 100 / 0.0244... is 100.00000000000001, which the weight CHECK refuses (QA,
    2026-10-16; seen on I3304, I3305 and I3350 Final)."""
    small = 0.024477102685254104
    pinned = pin_weights({"a": 100 - small, "b": small}, {"a": 0})

    assert pinned == {"a": 0, "b": 100.0}
