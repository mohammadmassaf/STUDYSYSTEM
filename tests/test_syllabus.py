"""A course with no past papers (3.9; D-218, D-221 - D-224), on I3351's shape: a Partial (30%,
~2026-11-16) and a Final (70%, ~2027-01-18), both in term, 9 learning outcomes, Unit 1 noted."""

import datetime
import json

import pytest
from sqlalchemy import select

from studysystem.db.tables import assessment, course, coverage, topic
from studysystem.errors import StudyError
from studysystem.services.courses import set_course_input
from studysystem.services.plan import get_plan
from studysystem.services.statements import add_instructor_statement
from studysystem.services.syllabus import add_syllabus_topics
from studysystem.services.units import write_unit
from tests.test_plan import _chapter, _course, _exam, _noted, _resit, _study_items, _tagged

TODAY = datetime.date(2026, 11, 1)
OUTCOMES = [
    "LO1 Explain the role of a system administrator",
    "LO2 Install Linux and manage software",
    "LO3 Manage boot, services and processes with systemd",
    "LO4 Manage files, users, groups, permissions and ACLs",
    "LO5 Configure storage, LVM, RAID and backups",
    "LO6 Configure networking and core network services",
    "LO7 Deploy an Active Directory-compatible domain",
    "LO8 Automate tasks with shell scripts and scheduled jobs",
    "LO9 Secure, log and monitor a Linux server",
]


def _i3351(engine, uid, prep_from=None):
    """I3351 with its two exams, in term unless `prep_from` is given (both exams, D-119).
    Returns (course_id, final, partial) - the two assessment ids."""
    with write_unit(engine) as conn:
        course_id = _course(conn, uid, "I3351", credits=5.0)
        _, final = _exam(conn, uid, course_id, prep_from=prep_from)
        partial_args = {"weight": 30.0, "date": "2026-11-16", "prep_from": prep_from}
        _, partial = _exam(conn, uid, course_id, "Partial exam", **partial_args)
    return course_id, final, partial


def _unit_1_on(engine, uid, course_id, topic_id):
    """Unit 1 fed, tagged to `topic_id` and noted: taught (D-120)."""
    with write_unit(engine) as conn:
        unit = _chapter(conn, uid, course_id)
        _tagged(conn, uid, topic_id, unit)
        _noted(conn, uid, unit)


def _ids(result):
    return {t["name"][:3]: t["topic_id"] for t in result["topics"]}


def _exam_part(reason, name):
    """The part of a study reason about one exam, e.g. "Final exam: 7.8 of 100 marks (...)"."""
    return next(part for part in reason.split(" + ") if name in part)


# --- study_add_syllabus_topics (D-224) -------------------------------------------------------


def test_nine_outcomes_become_active_topics_edged_to_the_final_only(service_engine, user_id):
    _, final, _ = _i3351(service_engine, user_id)

    result = add_syllabus_topics(service_engine, user_id, "I3351", "final exam", OUTCOMES)

    with service_engine.connect() as conn:
        rows = conn.execute(select(topic.c.status, topic.c.proposed_by)).all()
        edges = conn.execute(select(coverage.c.assessment_id)).scalars().all()
    assert rows == [("active", "syllabus")] * 9
    assert sorted(edges) == [final] * 9  # none to the Partial (D-222)
    assert result["assessment"] == "Final exam"
    assert result["stored"].startswith("I3351: 9 syllabus topics, each counted on the Final exam")


def test_a_resit_of_the_final_gets_an_edge_too(service_engine, user_id):
    _, final, _ = _i3351(service_engine, user_id)
    with write_unit(service_engine) as conn:
        slot_id = conn.execute(
            select(assessment.c.slot_id).where(assessment.c.id == final)
        ).scalar_one()
        _resit(conn, user_id, slot_id, 70.0)

    add_syllabus_topics(service_engine, user_id, "I3351", "Final exam", OUTCOMES[:1])

    with service_engine.connect() as conn:
        sittings = conn.execute(
            select(assessment.c.id).where(assessment.c.slot_id == slot_id)
        ).scalars()
        edges = conn.execute(select(coverage.c.assessment_id)).scalars().all()
        assert sorted(edges) == sorted(sittings)  # both sittings, one edge each


def test_a_lab_is_refused_as_the_syllabus_exam(service_engine, user_id):
    course_id, _, _ = _i3351(service_engine, user_id)
    with write_unit(service_engine) as conn:
        _exam(conn, user_id, course_id, "Lab", weight=None, kind="lab")

    with pytest.raises(StudyError) as refused:
        add_syllabus_topics(service_engine, user_id, "I3351", "Lab", OUTCOMES)

    assert refused.value.code == "invalid_value"


def test_a_repeated_name_is_refused_before_anything_is_read(service_engine, user_id):
    _i3351(service_engine, user_id)

    with pytest.raises(StudyError) as refused:
        add_syllabus_topics(service_engine, user_id, "I3351", "Final exam", ["LO1 x", "lo1 X"])

    [error] = refused.value.field_errors
    assert error == {"field": "outcomes[1]", "problem": "same as outcomes[0]"}


def test_a_second_call_with_a_known_outcome_writes_nothing(service_engine, user_id):
    _i3351(service_engine, user_id)
    add_syllabus_topics(service_engine, user_id, "I3351", "Final exam", OUTCOMES[:2])

    with pytest.raises(StudyError) as refused:
        add_syllabus_topics(service_engine, user_id, "I3351", "Final exam", OUTCOMES[1:3])

    assert [e["field"] for e in refused.value.field_errors] == ["outcomes[0]"]
    with service_engine.connect() as conn:
        assert len(conn.execute(select(topic.c.id)).all()) == 2  # LO3 rolled back with it


# --- the plan: the split rule (D-221, D-222) ---------------------------------------------------


def test_a_taught_outcome_takes_a_ninth_of_the_final_and_all_of_the_partial(
    service_engine, user_id
):
    course_id, _, _ = _i3351(service_engine, user_id)
    ids = _ids(add_syllabus_topics(service_engine, user_id, "I3351", "Final exam", OUTCOMES))
    _unit_1_on(service_engine, user_id, course_id, ids["LO1"])

    study = _study_items(get_plan(service_engine, user_id, TODAY))

    [lo1] = [item for name, item in study.items() if name.startswith("LO1")]
    assert len(study) == 1  # the 8 untaught outcomes wait for their units (D-118)
    final = _exam_part(lo1["reason"], "Final exam")
    assert "7.8 of 100 marks (11.1% - equal split of 9, no profile, inferred)" in final
    partial = _exam_part(lo1["reason"], "Partial exam")
    assert "30.0 of 100 marks (100.0% - equal split of 1, no profile, inferred)" in partial


def test_a_stated_share_replaces_the_equal_split_in_the_next_plan(service_engine, user_id):
    course_id, _, _ = _i3351(service_engine, user_id)
    ids = _ids(add_syllabus_topics(service_engine, user_id, "I3351", "Final exam", OUTCOMES))
    _unit_1_on(service_engine, user_id, course_id, ids["LO1"])
    before = _study_items(get_plan(service_engine, user_id, TODAY))

    reply = add_instructor_statement(
        service_engine,
        user_id,
        "I3351",
        "Final exam",
        "topic-marks",
        {"marks": 20, "of_total": 70},
        ids["LO1"],
    )
    after = _study_items(get_plan(service_engine, user_id, TODAY))

    [name] = after
    assert reply["stated"].endswith("The next plan uses it - this exam has no profile.")
    assert "20.0 of 100 marks (28.6% stated by the instructor, declared)" in after[name]["reason"]
    assert after[name]["score"] > before[name]["score"]


def test_the_other_outcomes_split_what_the_stated_share_leaves(service_engine, user_id):
    course_id, _, _ = _i3351(service_engine, user_id)
    ids = _ids(add_syllabus_topics(service_engine, user_id, "I3351", "Final exam", OUTCOMES))
    _unit_1_on(service_engine, user_id, course_id, ids["LO1"])
    add_instructor_statement(
        service_engine,
        user_id,
        "I3351",
        "Final exam",
        "topic-marks",
        {"marks": 20, "of_total": 70},
        ids["LO4"],  # untaught: it still takes its share out of the pool
    )

    [lo1] = _study_items(get_plan(service_engine, user_id, TODAY)).values()

    final = _exam_part(lo1["reason"], "Final exam")
    assert "(8.9% - equal split of 8 after the stated shares, no profile, inferred)" in final


def test_in_prep_an_untaught_outcome_comes_back_on_the_final_but_never_on_the_partial(
    service_engine, user_id
):
    # D-222: prep turns the taught filter off, but only LO1's chapter reaches the Partial.
    course_id, _, _ = _i3351(service_engine, user_id, prep_from="2026-10-25")
    ids = _ids(add_syllabus_topics(service_engine, user_id, "I3351", "Final exam", OUTCOMES))
    _unit_1_on(service_engine, user_id, course_id, ids["LO1"])

    study = _study_items(get_plan(service_engine, user_id, TODAY, shown_n=50))

    on_partial = sorted(name[:3] for name, item in study.items() if "Partial" in item["reason"])
    assert (len(study), on_partial) == (9, ["LO1"])
    [lo9] = [item for name, item in study.items() if name.startswith("LO9")]
    assert "7.8 of 100 marks (11.1% - equal split of 9" in lo9["reason"]


# --- the tool ----------------------------------------------------------------------------------


def test_the_tool_adds_the_outcomes_and_refuses_a_repeat_as_an_error_result(
    call, service_engine, user_id
):
    _i3351(service_engine, user_id)
    args = {"code": "I3351", "assessment": "Final exam", "outcomes": OUTCOMES[:2]}

    added = call("study_add_syllabus_topics", args)
    again = call("study_add_syllabus_topics", args)

    assert not added.is_error
    assert len(json.loads(added.content[0].text)["topics"]) == 2
    assert again.is_error
    assert json.loads(again.content[0].text)["code"] == "invalid_value"


# --- no_past_papers (D-223) --------------------------------------------------------------------


def test_a_paperless_course_asks_for_no_papers_of_either_kind(service_engine, user_id):
    course_id, _, _ = _i3351(service_engine, user_id)
    ids = _ids(add_syllabus_topics(service_engine, user_id, "I3351", "Final exam", OUTCOMES))
    _unit_1_on(service_engine, user_id, course_id, ids["LO1"])

    def kinds():
        result = get_plan(service_engine, user_id, TODAY, shown_n=50)
        return {item["setup_kind"] for item in result["lanes"]["setup"]["items"]}

    assert {"past-papers", "more-papers"} & kinds() == {"past-papers"}  # one hunt per slot
    set_course_input(service_engine, user_id, "I3351", "no_past_papers", True)
    assert {"past-papers", "more-papers"} & kinds() == set()


def test_no_past_papers_is_a_flag_that_can_be_undone(service_engine, user_id):
    _i3351(service_engine, user_id)

    reply = set_course_input(service_engine, user_id, "I3351", "no_past_papers", "true")
    set_course_input(service_engine, user_id, "I3351", "no_past_papers", False)

    assert (reply["value"], "tier" in reply) == (True, False)
    with service_engine.connect() as conn:
        assert conn.execute(select(course.c.no_past_papers)).scalar_one() == 0
