"""Exam profiles (1.13; D-14, D-67 - D-69). The weight math first: a pure function over papers
given newest first - no database. Then `derive_profile`, the write unit around it."""

import json

import pytest
from sqlalchemy import func, insert, select, update

from studysystem.db.tables import (
    exam_profile,
    generation_task,
    observation,
    past_exam,
    practice_item,
    profile_claim,
    topic,
    topic_weight,
)
from studysystem.errors import StudyError
from studysystem.services.assessments import add_assessment
from studysystem.services.courses import add_course, set_course_input
from studysystem.services.generation import start_generation, submit_generation
from studysystem.services.ids import new_id, now
from studysystem.services.past_exams import add_past_exam
from studysystem.services.profiles import derive_profile, topic_weights
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
    """Format from the newest paper only; instructor claims from papers by who teaches now."""
    result = derive_profile(service_engine, user_id, "I3302", FINAL)

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
