"""Scheduler v1 (1.15; D-82 - D-95). The score first: a pure function, no database. Then the
study lane's SQL, `_study_rows`, on fixture rows built directly - each test makes only the rows
its case needs."""

import datetime
import json
import logging
from types import SimpleNamespace

import pytest
from sqlalchemy import func, insert, select, update

from studysystem.db.tables import (
    assessment,
    assessment_slot,
    course,
    coverage,
    exam_profile,
    hours_entry,
    material,
    past_exam,
    plan,
    plan_item,
    topic,
    topic_state,
    topic_weight,
)
from studysystem.errors import StudyError
from studysystem.services.ids import new_id, now
from studysystem.services.plan import (
    _due_reviews,
    _setup_items,
    _study_rows,
    _topic_exams,
    get_plan,
    review_reason,
    setup_reason,
    study_reason,
    study_score,
)
from studysystem.services.units import write_unit

TODAY = datetime.date(2026, 10, 4)
SEM = "Semester 1 2026-2027"

# --- study_score: pure (D-84, D-86, D-89) ---------------------------------------------------


def test_mysql_today_scores_its_final_marks_over_days():
    # 70 x 39.3% = 27.5 marks x 4 credits = 110, Final in 106 days, never practised, 60 min.
    assert study_score([(110.0, 106)], 1.0, 60) == pytest.approx(1.038, abs=1e-3)


def test_a_topic_on_two_exams_adds_both():
    # 110 / 106 + 80 / 43 (D-89).
    assert study_score([(110.0, 106), (80.0, 43)], 1.0, 60) == pytest.approx(2.898, abs=1e-3)


def test_half_the_minutes_doubles_the_score():
    assert study_score([(110.0, 106)], 1.0, 30) == pytest.approx(
        2 * study_score([(110.0, 106)], 1.0, 60)
    )


def test_no_upcoming_exam_is_a_caller_bug_not_a_zero():
    # D-94 filters such a topic before scoring; 0 would claim it is worth nothing.
    with pytest.raises(ValueError):
        study_score([], 1.0, 60)


# --- _topic_exams: pure (D-84, D-86, D-89, D-95 - D-97) ------------------------------------


def _row(**changes):
    """One `_study_rows` row: MySQL on I3302's Final today, then whatever the case changes."""
    row = {
        "topic_id": "mysql",
        "topic_name": "MySQL",
        "course_id": "i3302",
        "code": "I3302",
        "credits": 4.0,
        "credits_tier": "declared",
        "assessment_id": "final",
        "exam_name": "Final exam",
        "exam_weight": 70.0,
        "exam_weight_tier": "declared",
        "date": "2027-01-18",
        "date_approx": 1,
        "exam_profile_id": "v2",
        "profile_version": 2,
        "profile_size": 7,
        "share": 39.34,
        "stability": None,
    }
    row.update(changes)
    return SimpleNamespace(**row)


def _exams(rows, today=TODAY, average_credits=4.0, latest=datetime.date(2027, 1, 18)):
    return _topic_exams(rows, today, average_credits, latest)


def test_mysql_today_is_one_exam_from_its_profile():
    [exam] = _exams([_row()])["mysql"]["exams"]

    assert exam["marks"] == pytest.approx(110.15, abs=0.01)  # 70 x 39.34% x 4
    assert (exam["days"], exam["share_source"], exam["date_source"]) == (
        106,
        "profile",
        "approximate",
    )


def test_a_topic_on_two_exams_merges_under_one_entry():
    # PHP sessions on the Final (v2) and a Partial with no profile that 3 topics reach.
    partial = {"assessment_id": "partial", "exam_name": "Partial exam", "exam_weight": 30.0}
    partial |= {"date": "2026-11-16", "exam_profile_id": None, "profile_version": None}
    partial |= {"profile_size": 0, "share": None}
    rows = [
        _row(topic_id="sessions", share=16.68),
        _row(topic_id="sessions", **partial),
        _row(topic_id="forms", **partial),
        _row(topic_id="cookies", **partial),
    ]

    topics = _exams(rows)

    final, on_partial = topics["sessions"]["exams"]
    assert final["marks"] == pytest.approx(46.70, abs=0.01)
    assert on_partial["marks"] == pytest.approx(40.0)  # 30 x 100/3 % x 4
    assert (on_partial["days"], on_partial["share_source"]) == (43, "equal split")
    assert topics["sessions"]["exam_profile_id"] is None  # Partial 0.93 beats Final 0.44 (D-97)


def test_a_topic_missing_from_the_profile_gets_its_average_share():
    [exam] = _exams([_row(topic_id="arrays", share=None)])["arrays"]["exams"]

    assert exam["share"] == pytest.approx(100 / 7)
    assert exam["marks"] == pytest.approx(40.0)  # 70 x 14.3% x 4
    assert exam["share_source"] == "average"


def test_unknown_credits_borrow_the_average():
    topic = _exams([_row(credits=None, credits_tier="unknown")], average_credits=3.5)["mysql"]

    assert (topic["credits"], topic["credits_tier"]) == (3.5, "inferred")
    assert topic["exams"][0]["marks"] == pytest.approx(70 * 0.3934 * 3.5)


def test_a_passed_approximate_date_counts_as_one_day():
    # Nov 18; the Partial's window opened Nov 16 (D-95).
    [exam] = _exams([_row(date="2026-11-16")], today=datetime.date(2026, 11, 18))["mysql"]["exams"]

    assert exam["days"] == 1


def test_a_missing_date_borrows_the_latest_exam_date():
    [exam] = _exams([_row(date=None, date_approx=0)])["mysql"]["exams"]

    assert (exam["days"], exam["date_source"]) == (106, "borrowed")


def test_an_unknown_weight_scores_as_thirty():
    [exam] = _exams([_row(exam_weight=None, exam_weight_tier="unknown")])["mysql"]["exams"]

    assert (exam["weight"], exam["weight_tier"]) == (30.0, "inferred")  # D-96


def test_the_profile_of_the_biggest_term_names_the_item():
    # Both exams profiled: Partial 40 / 43 beats Final 46.7 / 106 (D-97).
    rows = [
        _row(share=16.68),
        _row(
            assessment_id="partial",
            exam_weight=30.0,
            date="2026-11-16",
            exam_profile_id="p1",
            share=33.3,
        ),
    ]

    assert _exams(rows)["mysql"]["exam_profile_id"] == "p1"


def test_a_repeated_edge_does_not_score_the_exam_twice():
    assert len(_exams([_row(), _row()])["mysql"]["exams"]) == 1


def test_with_nothing_declared_anywhere_one_shared_value_is_used():
    # No course declares credits, no exam has a date: every topic is in the same case.
    [exam] = _exams(
        [_row(credits=None, credits_tier="unknown", date=None)], average_credits=None, latest=None
    )["mysql"]["exams"]

    assert exam["marks"] == pytest.approx(70 * 0.3934)
    assert (exam["days"], exam["date_source"]) == (1, "unknown")


# --- _study_rows: fixture builders ----------------------------------------------------------


def _course(conn, uid, code="I3302", credits=4.0, target_grade=75):
    course_id = new_id()
    conn.execute(
        insert(course).values(
            id=course_id,
            user_id=uid,
            code=code,
            name=code,
            instructor_tier="unknown",
            credits=credits,
            credits_tier="declared" if credits is not None else "unknown",
            target_grade=target_grade,
            semester_name=SEM,
            created_at=now(),
        )
    )
    return course_id


def _exam(
    conn,
    uid,
    course_id,
    name="Final exam",
    weight=70.0,
    date="2027-01-18",
    approx=1,
    status="upcoming",
    kind="exam",
    weight_tier=None,
):
    """One slot and its one assessment. Returns (slot_id, assessment_id)."""
    if weight_tier is None:
        weight_tier = "declared" if weight is not None else "unknown"
    slot_id, assessment_id = new_id(), new_id()
    conn.execute(
        insert(assessment_slot).values(
            id=slot_id,
            owner_id=uid,
            course_id=course_id,
            name=name,
            kind=kind,
            created_at=now(),
        )
    )
    conn.execute(
        insert(assessment).values(
            id=assessment_id,
            user_id=uid,
            slot_id=slot_id,
            weight=weight,
            weight_tier=weight_tier,
            date=date,
            date_approx=approx,
            session_type="first" if kind == "exam" else None,
            status=status,
            created_at=now(),
        )
    )
    return slot_id, assessment_id


def _topic(conn, uid, course_id, name, status="active"):
    topic_id = new_id()
    conn.execute(
        insert(topic).values(
            id=topic_id,
            user_id=uid,
            course_id=course_id,
            name=name,
            status=status,
            proposed_by="profile",
            status_changed_at=now(),
            created_at=now(),
        )
    )
    return topic_id


def _edge(conn, uid, topic_id, assessment_id):
    conn.execute(
        insert(coverage).values(
            id=new_id(),
            user_id=uid,
            topic_id=topic_id,
            assessment_id=assessment_id,
            tier="inferred",
            created_at=now(),
        )
    )


def _profile(conn, uid, slot_id, version, weights):
    """A profile version of `slot_id` with {topic_id: weight}. Returns its id."""
    profile_id = new_id()
    conn.execute(
        insert(exam_profile).values(
            id=profile_id,
            owner_id=uid,
            slot_id=slot_id,
            version=version,
            derived_at=now(),
            evidence_count_syllabus=0,
            evidence_count_instructor=0,
            evidence_count_format=0,
        )
    )
    for topic_id, weight in weights.items():
        conn.execute(
            insert(topic_weight).values(
                id=new_id(),
                exam_profile_id=profile_id,
                topic_id=topic_id,
                weight=weight,
                created_at=now(),
            )
        )
    return profile_id


def _rows(engine, uid):
    with engine.connect() as conn:
        return _study_rows(conn, uid, TODAY)


def _by_topic(rows):
    out = {}
    for row in rows:
        out.setdefault(row.topic_name, []).append(row)
    return out


# --- _study_rows ----------------------------------------------------------------------------


def test_only_the_latest_profile_gives_the_share(service_engine, user_id):
    # v1 and v2 both weight MySQL; joining both would double the topic.
    with write_unit(service_engine) as conn:
        course_id = _course(conn, user_id)
        slot_id, final = _exam(conn, user_id, course_id)
        mysql = _topic(conn, user_id, course_id, "MySQL")
        _edge(conn, user_id, mysql, final)
        _profile(conn, user_id, slot_id, 1, {mysql: 23.2})
        v2 = _profile(conn, user_id, slot_id, 2, {mysql: 39.3})

    rows = _rows(service_engine, user_id)

    assert len(rows) == 1
    assert rows[0].share == pytest.approx(39.3)
    assert (rows[0].exam_profile_id, rows[0].profile_version) == (v2, 2)


def test_an_exam_with_no_profile_keeps_its_row(service_engine, user_id):
    # The Partial: no papers, no profile. Its share is D-89's equal split, made later.
    with write_unit(service_engine) as conn:
        course_id = _course(conn, user_id)
        _, partial = _exam(conn, user_id, course_id, "Partial exam", 30.0, "2026-11-16")
        sessions = _topic(conn, user_id, course_id, "PHP sessions")
        _edge(conn, user_id, sessions, partial)

    [row] = _rows(service_engine, user_id)

    assert row.share is None
    assert row.exam_profile_id is None
    assert row.profile_size == 0  # a count over no profile is 0, not NULL


def test_a_topic_missing_from_the_latest_profile_keeps_its_row(service_engine, user_id):
    # On the Final by an edge, but not in v1 (a paper landed after the derive): D-89 gives it
    # 100 / profile_size, counted over the profile, not over these rows.
    with write_unit(service_engine) as conn:
        course_id = _course(conn, user_id)
        slot_id, final = _exam(conn, user_id, course_id)
        mysql = _topic(conn, user_id, course_id, "MySQL")
        files = _topic(conn, user_id, course_id, "files")
        arrays = _topic(conn, user_id, course_id, "arrays")
        for topic_id in (mysql, files, arrays):
            _edge(conn, user_id, topic_id, final)
        v1 = _profile(conn, user_id, slot_id, 1, {mysql: 60.0, files: 40.0})

    rows = _by_topic(_rows(service_engine, user_id))

    [arrays_row] = rows["arrays"]
    assert arrays_row.share is None
    assert arrays_row.exam_profile_id == v1
    assert arrays_row.profile_size == 2  # MySQL and files - arrays itself is not counted
    assert rows["MySQL"][0].profile_size == 2


def test_only_an_exact_passed_date_drops_the_exam(service_engine, user_id):
    # D-95: today counts, an approximate passed date counts, a NULL date counts.
    with write_unit(service_engine) as conn:
        course_id = _course(conn, user_id)
        mysql = _topic(conn, user_id, course_id, "MySQL")
        for name, date, approx in [
            ("exact passed", "2026-10-01", 0),
            ("approx passed", "2026-10-01", 1),
            ("no date", None, 0),
            ("exact today", "2026-10-04", 0),
        ]:
            _, assessment_id = _exam(conn, user_id, course_id, name, 25.0, date, approx)
            _edge(conn, user_id, mysql, assessment_id)

    rows = _rows(service_engine, user_id)

    assert sorted(row.exam_name for row in rows) == ["approx passed", "exact today", "no date"]


def test_a_sat_exam_and_a_project_are_not_planned(service_engine, user_id):
    with write_unit(service_engine) as conn:
        course_id = _course(conn, user_id)
        mysql = _topic(conn, user_id, course_id, "MySQL")
        _, sat = _exam(conn, user_id, course_id, "Partial exam", status="sat")
        _, project = _exam(conn, user_id, course_id, "Project", kind="project")
        _, final = _exam(conn, user_id, course_id)
        for assessment_id in (sat, project, final):
            _edge(conn, user_id, mysql, assessment_id)

    rows = _rows(service_engine, user_id)

    assert [row.exam_name for row in rows] == ["Final exam"]


def test_only_the_users_active_topics_count(service_engine, user_id, other_user_id):
    with write_unit(service_engine) as conn:
        course_id = _course(conn, user_id)
        _, final = _exam(conn, user_id, course_id)
        for name, status in [("MySQL", "active"), ("new", "proposed"), ("gone", "declined")]:
            _edge(conn, user_id, _topic(conn, user_id, course_id, name, status), final)

        theirs = _course(conn, other_user_id)
        _, their_final = _exam(conn, other_user_id, theirs)
        _edge(conn, other_user_id, _topic(conn, other_user_id, theirs, "theirs"), their_final)

    rows = _rows(service_engine, user_id)

    assert [row.topic_name for row in rows] == ["MySQL"]


def test_a_topic_on_two_exams_gives_two_rows(service_engine, user_id):
    with write_unit(service_engine) as conn:
        course_id = _course(conn, user_id)
        _, final = _exam(conn, user_id, course_id)
        _, partial = _exam(conn, user_id, course_id, "Partial exam", 30.0, "2026-11-16")
        sessions = _topic(conn, user_id, course_id, "PHP sessions")
        _edge(conn, user_id, sessions, final)
        _edge(conn, user_id, sessions, partial)

    rows = _rows(service_engine, user_id)

    assert sorted((row.topic_name, row.exam_name) for row in rows) == [
        ("PHP sessions", "Final exam"),
        ("PHP sessions", "Partial exam"),
    ]


def test_a_topic_with_no_state_keeps_its_row(service_engine, user_id):
    # 0 topic_state rows live today; an inner join would empty the study lane.
    with write_unit(service_engine) as conn:
        course_id = _course(conn, user_id)
        _, final = _exam(conn, user_id, course_id)
        mysql = _topic(conn, user_id, course_id, "MySQL")
        files = _topic(conn, user_id, course_id, "files")
        _edge(conn, user_id, mysql, final)
        _edge(conn, user_id, files, final)
        conn.execute(
            insert(topic_state).values(
                user_id=user_id,
                topic_id=files,
                stability=3.5,
                last_reviewed_at="2026-10-01T18:00:00Z",
                reps=1,
                lapses=0,
                updated_at=now(),
            )
        )

    rows = _by_topic(_rows(service_engine, user_id))

    assert rows["MySQL"][0].stability is None
    assert rows["files"][0].stability == pytest.approx(3.5)


# --- _setup_items (D-22, D-85, D-98, D-99) -------------------------------------------------


def _sha():
    return (new_id() * 3)[:64]


def _material(conn, uid, course_id):
    conn.execute(
        insert(material).values(
            id=new_id(),
            user_id=uid,
            course_id=course_id,
            filename="Chapter1.pdf",
            file_ref="files/x",
            content_sha256=_sha(),
            media_type="application/pdf",
            kind="chapter",
            added_at=now(),
        )
    )


def _papers(conn, uid, slot_id, n):
    for year in range(2010, 2010 + n):
        conn.execute(
            insert(past_exam).values(
                id=new_id(),
                owner_id=uid,
                slot_id=slot_id,
                session_type="first",
                session_date=f"{year}-01-20",
                instructor_tier="unknown",
                file_ref="files/x",
                content_sha256=_sha(),
                created_at=now(),
            )
        )


def _hours(conn, uid, course_id, key, occurred_at, voided_at=None):
    conn.execute(
        insert(hours_entry).values(
            id=new_id(),
            user_id=uid,
            course_id=course_id,
            setup_key=key,
            minutes=20,
            occurred_at=occurred_at,
            source="tool",
            voided_at=voided_at,
            created_at=now(),
        )
    )


def _setup(engine, uid):
    with engine.connect() as conn:
        return _setup_items(conn, uid, TODAY)


def _of(out, kind):
    return {item["setup_key"]: item for item in out["items"] if item["setup_kind"] == kind}


def test_unknown_credits_raise_an_item_and_borrow_the_average(service_engine, user_id):
    with write_unit(service_engine) as conn:
        unknown = _course(conn, user_id, "I3305", credits=None)
        _exam(conn, user_id, unknown)  # Final 70
        _course(conn, user_id, "I3350", credits=5.0)  # the only declared credits: average 5

    out = _setup(service_engine, user_id)

    assert _of(out, "credits")[f"credits:{unknown}"]["marks_unlocked_estimate"] == 0
    first = _of(out, "first-material")[f"first-material:{unknown}"]
    assert (first["credits"], first["marks_unlocked_estimate"]) == (5.0, 350.0)  # 70 x 5


def test_a_missing_target_grade_raises_an_item_worth_nothing(service_engine, user_id):
    with write_unit(service_engine) as conn:
        course_id = _course(conn, user_id, target_grade=None)

    item = _of(_setup(service_engine, user_id), "target-grade")[f"target-grade:{course_id}"]

    assert item["marks_unlocked_estimate"] == 0  # target grade feeds the shortfall check (D-85)


def test_guessed_weights_raise_one_grading_item_per_course(service_engine, user_id):
    with write_unit(service_engine) as conn:
        course_id = _course(conn, user_id)
        _exam(conn, user_id, course_id, weight=100.0, weight_tier="inferred")  # D-15's default
        _exam(conn, user_id, course_id, "Partial exam", weight=None)  # unknown -> 30 (D-96)

    grading = _of(_setup(service_engine, user_id), "grading")

    assert list(grading) == [f"grading:{course_id}"]
    assert grading[f"grading:{course_id}"]["marks_unlocked_estimate"] == (100 + 30) * 4


def test_an_upcoming_exam_with_no_date_raises_its_own_item(service_engine, user_id):
    with write_unit(service_engine) as conn:
        course_id = _course(conn, user_id)
        _, undated = _exam(conn, user_id, course_id, date=None, approx=0)
        _exam(conn, user_id, course_id, "Partial exam", date=None, approx=0, status="sat")

    dates = _of(_setup(service_engine, user_id), "exam-date")

    assert list(dates) == [f"exam-date:{undated}"]
    assert dates[f"exam-date:{undated}"]["marks_unlocked_estimate"] == 70 * 4


def test_first_material_waits_for_a_material_or_a_live_topic(service_engine, user_id):
    with write_unit(service_engine) as conn:
        cold = _course(conn, user_id, "I3350")
        fed = _course(conn, user_id, "I3301")
        _material(conn, user_id, fed)
        topical = _course(conn, user_id, "I3302")
        _topic(conn, user_id, topical, "MySQL")
        declined_only = _course(conn, user_id, "I3303")
        _topic(conn, user_id, declined_only, "gone", status="declined")

    keys = set(_of(_setup(service_engine, user_id), "first-material"))

    assert keys == {f"first-material:{cold}", f"first-material:{declined_only}"}


def test_past_papers_are_worth_the_weight_until_the_third_and_stop_at_the_fifth(
    service_engine, user_id
):
    slots = {}
    with write_unit(service_engine) as conn:
        course_id = _course(conn, user_id)
        for n in (0, 2, 3, 5):
            slots[n], _ = _exam(conn, user_id, course_id, f"slot {n}", weight=25.0)
            _papers(conn, user_id, slots[n], n)

    papers = _of(_setup(service_engine, user_id), "past-papers")

    worth = {key: item["marks_unlocked_estimate"] for key, item in papers.items()}
    assert worth == {
        f"past-papers:{slots[0]}": 100.0,  # 25 x 4
        f"past-papers:{slots[2]}": 100.0,
        f"past-papers:{slots[3]}": 0.0,  # papers 4-5 unlock only the mock gate (D-24)
    }


def test_a_sat_exam_and_a_project_ask_for_no_papers(service_engine, user_id):
    with write_unit(service_engine) as conn:
        course_id = _course(conn, user_id)
        _exam(conn, user_id, course_id, "Partial exam", status="sat")
        _exam(conn, user_id, course_id, "Project", kind="project")

    assert _of(_setup(service_engine, user_id), "past-papers") == {}


def test_logged_hours_snooze_a_key_for_seven_days(service_engine, user_id):
    # Today 2026-10-04: the cutoff is 2026-09-27, and a voided entry snoozes nothing.
    cases = {
        "3 days ago": ("2026-10-01T18:00:00Z", None),
        "8 days ago": ("2026-09-26T18:00:00Z", None),
        "voided": ("2026-10-01T18:00:00Z", "2026-10-01T19:00:00Z"),
        "exactly 7": ("2026-09-27T09:00:00Z", None),
    }
    keys = {}
    with write_unit(service_engine) as conn:
        for name, (occurred_at, voided_at) in cases.items():
            course_id = _course(conn, user_id, name, target_grade=None)
            keys[name] = f"target-grade:{course_id}"
            _hours(conn, user_id, course_id, keys[name], occurred_at, voided_at)

    out = _setup(service_engine, user_id)

    assert set(_of(out, "target-grade")) == {keys["8 days ago"], keys["voided"]}
    assert out["filtered"] == 2


def test_two_plans_raise_the_same_keys(service_engine, user_id):
    # Done when: a setup item raised by two plans carries the same setup_key.
    with write_unit(service_engine) as conn:
        course_id = _course(conn, user_id, target_grade=None)
        _exam(conn, user_id, course_id, date=None, approx=0)

    first = [item["setup_key"] for item in _setup(service_engine, user_id)["items"]]
    second = [item["setup_key"] for item in _setup(service_engine, user_id)["items"]]

    assert first and sorted(first) == sorted(second)


def test_an_item_vanishes_once_its_input_is_declared(service_engine, user_id):
    # Done when: ... and it vanishes once its input is declared.
    with write_unit(service_engine) as conn:
        course_id = _course(conn, user_id, target_grade=None)
    key = f"target-grade:{course_id}"
    assert key in _of(_setup(service_engine, user_id), "target-grade")

    with write_unit(service_engine) as conn:
        conn.execute(update(course).where(course.c.id == course_id).values(target_grade=80))

    assert key not in _of(_setup(service_engine, user_id), "target-grade")


# --- _due_reviews (D-15, D-87, D-94) --------------------------------------------------------


def _state(conn, uid, topic_id, due_at):
    conn.execute(
        insert(topic_state).values(
            user_id=uid,
            topic_id=topic_id,
            stability=2.0,
            due_at=due_at,
            last_reviewed_at="2026-09-30T18:00:00Z",
            reps=1,
            lapses=0,
            updated_at=now(),
        )
    )


def _reviews(engine, uid):
    with engine.connect() as conn:
        return [row.topic_name for row in _due_reviews(conn, uid, TODAY)]


def test_due_reviews_are_today_or_earlier_most_overdue_first(service_engine, user_id):
    with write_unit(service_engine) as conn:
        course_id = _course(conn, user_id)
        for name, due_at in [
            ("today late", "2026-10-04T23:30:00Z"),
            ("last week", "2026-09-27T08:00:00Z"),
            ("yesterday", "2026-10-03T08:00:00Z"),
            ("tomorrow", "2026-10-05T00:00:00Z"),
            ("never reviewed", None),
        ]:
            _state(conn, user_id, _topic(conn, user_id, course_id, name), due_at)

    assert _reviews(service_engine, user_id) == ["last week", "yesterday", "today late"]


def test_only_the_users_active_topics_are_reviewed(service_engine, user_id, other_user_id):
    due = "2026-10-01T08:00:00Z"
    with write_unit(service_engine) as conn:
        course_id = _course(conn, user_id)
        _state(conn, user_id, _topic(conn, user_id, course_id, "MySQL"), due)
        _state(conn, user_id, _topic(conn, user_id, course_id, "agile", "unexamined"), due)
        theirs = _course(conn, other_user_id)
        _state(conn, other_user_id, _topic(conn, other_user_id, theirs, "theirs"), due)

    assert _reviews(service_engine, user_id) == ["MySQL"]


# --- reasons: pure (ticket 13, D-12) ---------------------------------------------------------


def test_mysqls_reason_names_every_guessed_input():
    entry = _exams([_row()])["mysql"]

    assert study_reason(entry, 1.0, 60) == (
        "I3302 MySQL: Final exam: 27.5 of 100 marks (profile v2), in 106 days (approximate date)"
        " · x 4 credits · no attempts yet (weakness 1, inferred) · 60 min estimate (inferred)"
    )


def test_a_two_exam_reason_names_both_and_how_each_share_was_made():
    partial = {"assessment_id": "partial", "exam_name": "Partial exam", "exam_weight": 30.0}
    partial |= {"date": "2026-11-16", "exam_profile_id": None, "share": None, "profile_size": 0}
    entry = _exams([_row(), _row(**partial)])["mysql"]

    reason = study_reason(entry, 1.0, 60)

    assert "Final exam: 27.5 of 100 marks (profile v2)" in reason
    assert "Partial exam: 30.0 of 100 marks (equal split, no profile), in 43 days" in reason


def test_inferred_inputs_are_named():
    rows = [
        _row(
            credits=None,
            credits_tier="unknown",
            share=None,
            date=None,
            date_approx=0,
            exam_weight=None,
            exam_weight_tier="unknown",
        )
    ]
    reason = study_reason(_exams(rows, average_credits=3.75)["mysql"], 1.0, 60)

    assert "average share, inferred" in reason
    assert "exam weight 30, inferred" in reason  # D-96
    assert "no date - the latest exam date borrowed" in reason
    assert "x 3.75 credits (average of declared courses, inferred)" in reason


def test_a_reached_approximate_date_asks_for_the_exact_one():
    entry = _exams([_row(date="2026-11-16")], today=datetime.date(2026, 11, 18))["mysql"]

    assert "enter the exact date" in study_reason(entry, 1.0, 60)  # D-95


def _item(**changes):
    item = {
        "setup_kind": "first-material",
        "setup_key": "first-material:i3350",
        "target_name": None,
        "code": "I3350",
        "marks_unlocked_estimate": 500.0,
    }
    item.update(changes)
    return item


def test_setup_reasons_say_what_to_get_and_what_it_could_move():
    assert setup_reason(_item()) == (
        "I3350: add the first material - nothing to study from yet · could move 500 (marks x"
        " credits)"
    )
    papers = setup_reason(
        _item(setup_kind="past-papers", target_name="Final exam", marks_unlocked_estimate=0.0)
    )
    assert papers.startswith("I3350 Final exam: get more past papers")
    assert "mock-exam gate" in papers
    assert "moves no marks itself" in setup_reason(
        _item(setup_kind="target-grade", marks_unlocked_estimate=0.0)
    )


def test_a_review_reason_says_since_when():
    row = SimpleNamespace(code="I3302", topic_name="PHP sessions", due_at="2026-10-01T08:00:00Z")

    assert review_reason(row) == "I3302: PHP sessions - review due since 2026-10-01"


# --- get_plan: the write unit (D-12, D-22, D-92, D-100) --------------------------------------


def _i3302_and_a_cold_course(conn, uid):
    """I3302 with a profiled Final over three topics, and I3350 with nothing yet."""
    i3302 = _course(conn, uid, "I3302")
    slot_id, final = _exam(conn, uid, i3302)
    _exam(conn, uid, i3302, "Partial exam", 30.0, "2026-11-16")
    ids = {name: _topic(conn, uid, i3302, name) for name in ("MySQL", "sessions", "regex")}
    for topic_id in ids.values():
        _edge(conn, uid, topic_id, final)
    _profile(conn, uid, slot_id, 1, {ids["MySQL"]: 60.0, ids["sessions"]: 30.0, ids["regex"]: 10.0})
    _papers(conn, uid, slot_id, 5)
    i3350 = _course(conn, uid, "I3350", credits=5.0)
    _exam(conn, uid, i3350)
    return ids


def _plan_rows(engine, plan_id):
    with engine.connect() as conn:
        return conn.execute(
            select(plan_item).where(plan_item.c.plan_id == plan_id).order_by(plan_item.c.rank)
        ).all()


def test_a_plan_ranks_every_lane_and_saves_the_whole_ranking(service_engine, user_id):
    with write_unit(service_engine) as conn:
        _i3302_and_a_cold_course(conn, user_id)

    result = get_plan(service_engine, user_id, TODAY, shown_n=2)

    study = result["lanes"]["study"]
    assert [item["topic_name"] for item in study["items"]] == ["MySQL", "sessions"]
    assert study["hidden"] == 1
    assert study["items"][0]["reason"].startswith(
        "I3302 MySQL: Final exam: 42.0 of 100 marks (profile v1)"
    )
    setup = result["lanes"]["setup"]
    # I3350 first-material 70 x 5 = 350, then I3350 Final papers 350 -> the key breaks the tie.
    assert [item["setup_kind"] for item in setup["items"]] == ["first-material", "past-papers"]
    assert result["lanes"]["review"] == {"items": [], "hidden": 0}

    rows = _plan_rows(service_engine, result["plan_id"])
    by_lane = {lane: [r for r in rows if r.lane == lane] for lane in ("study", "setup")}
    assert [r.rank for r in by_lane["study"]] == [1, 2, 3]
    assert [r.shown for r in by_lane["study"]] == [1, 1, 0]  # only what was returned (D-92)
    assert all(r.estimated_minutes == 60 and r.exam_profile_id for r in by_lane["study"])
    assert len(by_lane["setup"]) == setup["hidden"] + 2


def test_every_call_writes_a_new_plan(service_engine, user_id):
    # Done when: a plan row is written on each invocation (D-12).
    with write_unit(service_engine) as conn:
        _i3302_and_a_cold_course(conn, user_id)

    first = get_plan(service_engine, user_id, TODAY)["plan_id"]
    second = get_plan(service_engine, user_id, TODAY)["plan_id"]

    with service_engine.connect() as conn:
        snapshots = conn.execute(select(plan.c.id, plan.c.strategy_snapshot)).all()
    assert first != second and {row.id for row in snapshots} == {first, second}
    snapshot = json.loads(snapshots[0].strategy_snapshot)
    assert len(snapshot) == 2
    assert all(
        v == {"strategy": "default", "conceded": False, "target_grade": 75}
        for v in snapshot.values()
    )


def test_equal_scores_rank_by_topic_id(service_engine, user_id):
    with write_unit(service_engine) as conn:
        course_id = _course(conn, user_id)
        slot_id, final = _exam(conn, user_id, course_id)
        ids = sorted(_topic(conn, user_id, course_id, name) for name in ("b", "a"))
        for topic_id in ids:
            _edge(conn, user_id, topic_id, final)
        _profile(conn, user_id, slot_id, 1, dict.fromkeys(ids, 50.0))

    items = get_plan(service_engine, user_id, TODAY)["lanes"]["study"]["items"]

    assert [item["topic_id"] for item in items] == ids  # D-100


@pytest.mark.parametrize("shown_n", [0, -1, True, 2.5])
def test_shown_n_below_one_is_refused_before_anything_is_written(service_engine, user_id, shown_n):
    with pytest.raises(StudyError) as err:
        get_plan(service_engine, user_id, TODAY, shown_n=shown_n)

    assert err.value.field_errors[0]["field"] == "shown_n"
    with service_engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(plan)).scalar_one() == 0


def test_a_topic_with_review_state_fails_loudly_and_saves_nothing(service_engine, user_id):
    # Until 1.16 writes weakness from it (D-82, D-100): never a silent 1.0.
    with write_unit(service_engine) as conn:
        ids = _i3302_and_a_cold_course(conn, user_id)
        _state(conn, user_id, ids["MySQL"], "2026-10-10T08:00:00Z")

    with pytest.raises(StudyError) as err:
        get_plan(service_engine, user_id, TODAY)

    assert err.value.code == "weakness_not_built"
    with service_engine.connect() as conn:
        assert conn.execute(select(func.count()).select_from(plan)).scalar_one() == 0


def test_the_log_line_counts_what_the_plan_left_out(service_engine, user_id, caplog):
    with write_unit(service_engine) as conn:
        ids = _i3302_and_a_cold_course(conn, user_id)
        course_id = conn.execute(
            select(topic.c.course_id).where(topic.c.id == ids["MySQL"])
        ).scalar_one()
        _topic(conn, user_id, course_id, "no exam left")  # active, no upcoming exam (D-94)
        cold = conn.execute(select(course.c.id).where(course.c.code == "I3350")).scalar_one()
        _hours(conn, user_id, cold, f"first-material:{cold}", "2026-10-03T18:00:00Z")

    with caplog.at_level(logging.INFO, logger="studysystem.services.plan"):
        result = get_plan(service_engine, user_id, TODAY)

    [line] = [json.loads(r.message) for r in caplog.records if "get_plan" in r.message]
    assert line["plan_id"] == result["plan_id"]
    assert line["filtered"] == 2  # the snoozed key + the topic with no exam left
    assert line["lanes"] == {"review": 0, "study": 3, "setup": 2}
    assert line["candidates"] == 3 + 2 + 2
    assert isinstance(line["ms"], int)


def test_get_plan_through_the_tool(call, service_engine, user_id):
    with write_unit(service_engine) as conn:
        _i3302_and_a_cold_course(conn, user_id)

    result = call("study_get_plan", {"shown_n": 3})

    assert not result.is_error
    body = json.loads(result.content[0].text)
    assert len(body["lanes"]["study"]["items"]) == 3
    assert body["lanes"]["study"]["items"][0]["topic_name"] == "MySQL"


# --- after QA (D-95 in the setup lane, the D-95 note, the D-98 credits note) ----------------


def test_a_sat_exam_left_upcoming_asks_for_no_papers_after_its_exact_date(service_engine, user_id):
    # Sat on Oct 1, status never flipped: past its exact date, so not upcoming (D-95, D-99).
    with write_unit(service_engine) as conn:
        course_id = _course(conn, user_id)
        _exam(conn, user_id, course_id, "Partial exam", 30.0, "2026-10-01", approx=0)

    out = _setup(service_engine, user_id)

    assert _of(out, "past-papers") == {}
    assert _of(out, "first-material")[f"first-material:{course_id}"]["nearest_exam"] is None


def test_an_approximate_exam_tomorrow_is_not_called_reached():
    entry = _exams([_row(date="2026-10-05")])["mysql"]

    assert "enter the exact date" not in study_reason(entry, 1.0, 60)


def test_with_no_credits_declared_anywhere_the_reason_says_so():
    rows = [_row(credits=None, credits_tier="unknown")]
    reason = study_reason(_exams(rows, average_credits=None)["mysql"], 1.0, 60)

    assert "x 1 credits (no course declares credits yet - 1 used, D-98)" in reason
