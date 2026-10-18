"""Split and merge (2.11; D-192 - D-197, D-200, D-201, D-203) on a small I3302: `PHP + MySQL`
split in two, `PHP sessions` merged into `PHP cookies & login/logout` - the demo of D-196, with
fewer items. Every retired topic is read back through `lineage_violations`."""

import itertools
import json

import pytest
from sqlalchemy import func, insert, select, update

from studysystem.db.tables import (
    assessment,
    attempt,
    coverage,
    exam_profile,
    generation_task,
    material,
    past_exam,
    practice_item,
    topic,
    topic_split,
    topic_state,
    topic_weight,
)
from studysystem.errors import StudyError
from studysystem.services import study_loop
from studysystem.services.assessments import add_assessment
from studysystem.services.courses import add_course
from studysystem.services.ids import new_id, now
from studysystem.services.study_loop import start_session, submit_attempts
from studysystem.services.taxonomy import (
    lineage_violations,
    merge_topics,
    move_items,
    split_topic,
)
from studysystem.services.topics import edge_if_missing, get_topics, material_edge_if_missing
from studysystem.services.units import write_unit
from tests._client import tool_names

SEM = "Semester 1 2026-2027"
MYSQL = "PHP + MySQL (prepared statements)"
SESSIONS = "PHP sessions"
COOKIES = "PHP cookies & login/logout"
POSITIONS = itertools.count(1)


# --- a small I3302 ------------------------------------------------------------------------


def _topic(engine, uid, course_id, name, status="active"):
    topic_id = new_id()
    with write_unit(engine) as conn:
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


def _paper(engine, uid, slot_id, date):
    paper_id = new_id()
    with write_unit(engine) as conn:
        conn.execute(
            insert(past_exam).values(
                id=paper_id,
                owner_id=uid,
                slot_id=slot_id,
                session_type="first",
                session_date=date,
                instructor_tier="unknown",
                file_ref=f"files/{date}",
                content_sha256=date.replace("-", "").ljust(64, "a"),
                created_at=now(),
            )
        )
    return paper_id


def _items(engine, uid, topic_id, paper, marks):
    """One past-exam item of `paper` per mark, on `topic_id`."""
    task_id = new_id()
    ids = [new_id() for _ in marks]
    with write_unit(engine) as conn:
        conn.execute(
            insert(generation_task).values(
                id=task_id,
                user_id=uid,
                kind="transcription",
                scope_type="past-exam",
                scope_id=paper,
                route="host",
                created_at=now(),
            )
        )
        for n, (item_id, mark) in enumerate(zip(ids, marks, strict=True), start=1):
            conn.execute(
                insert(practice_item).values(
                    id=item_id,
                    user_id=uid,
                    topic_id=topic_id,
                    task_id=task_id,
                    item_ordinal=n,
                    origin="past-exam",
                    source_marker="from-material",
                    past_exam_id=paper,
                    position=next(POSITIONS),  # unique on any paper, across calls
                    question=f"part {n}",
                    answer_provenance="none",
                    marks=mark,
                    created_at=now(),
                )
            )
    return ids


def _material(engine, uid, course_id, filename):
    material_id = new_id()
    with write_unit(engine) as conn:
        conn.execute(
            insert(material).values(
                id=material_id,
                user_id=uid,
                course_id=course_id,
                filename=filename,
                file_ref=f"files/{filename}",
                content_sha256=filename.encode().hex().ljust(64, "0")[:64],
                media_type="application/pdf",
                has_text_layer=True,
                kind="chapter",
                added_at=now(),
            )
        )
    return material_id


@pytest.fixture
def i3302(service_engine, user_id):
    """Three active topics with their Final (and, for cookies, Partial) items and edges, as the
    accepts of 1.12 left them; Chapter 6 tagged to MySQL."""
    e, u = service_engine, user_id
    web = add_course(e, u, "I3302", "Server-Side Web Development", SEM)
    partial = add_assessment(e, u, "I3302", "Partial exam", "exam")
    final_paper = _paper(e, u, web["slot_id"], "2018-07-21")
    partial_paper = _paper(e, u, partial["slot_id"], "2018-11-15")

    mysql = _topic(e, u, web["course_id"], MYSQL)
    sessions = _topic(e, u, web["course_id"], SESSIONS)
    cookies = _topic(e, u, web["course_id"], COOKIES)
    world = {
        "course_id": web["course_id"],
        "final": web["assessment_id"],
        "partial": partial["assessment_id"],
        "mysql": mysql,
        "sessions": sessions,
        "cookies": cookies,
        "sql_items": _items(e, u, mysql, final_paper, [5, 5]),
        "page_items": _items(e, u, mysql, final_paper, [15, 25]),
        "session_items": _items(e, u, sessions, final_paper, [6, 7]),
        "cookie_items": _items(e, u, cookies, final_paper, [20])
        + _items(e, u, cookies, partial_paper, [2]),
        "ch6": _material(e, u, web["course_id"], "PHP_Chapter6_Eng_DataBase.pdf"),
    }
    with write_unit(e) as conn:
        for t in (mysql, sessions, cookies):
            edge_if_missing(conn, u, t, web["assessment_id"])
        edge_if_missing(conn, u, cookies, partial["assessment_id"])
        material_edge_if_missing(conn, u, mysql, world["ch6"])
    return world


def _attempt(engine, uid, *parts):
    """One graded problem, through the real writer: each part {practice_item_id | topic_id,
    correct}. Its replay writes the topic_state rows."""
    session = start_session(engine, uid, "I3302", "practice")["session_id"]
    return submit_attempts(engine, uid, "I3302", list(parts), session)


def _row(engine, table, **where):
    with engine.connect() as conn:
        query = select(table)
        for column, value in where.items():
            query = query.where(table.c[column] == value)
        return conn.execute(query).one_or_none()


def _rows(engine, table, **where):
    with engine.connect() as conn:
        query = select(table)
        for column, value in where.items():
            query = query.where(table.c[column] == value)
        return conn.execute(query).all()


def _count(engine, table):
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(table)).scalar_one()


def _lineage(engine, uid):
    with engine.connect() as conn:
        return lineage_violations(conn, uid)


def _two_children(w):
    return [
        {"name": "MySQL queries", "item_ids": w["sql_items"]},
        {"name": "PHP database apps", "item_ids": w["page_items"]},
    ]


def _refused(engine, uid, w, children) -> StudyError:
    with pytest.raises(StudyError) as err:
        split_topic(engine, uid, w["mysql"], children)
    # nothing written: the parent is still live and has no children
    assert _row(engine, topic, id=w["mysql"]).status == "active"
    assert _count(engine, topic_split) == 0
    return err.value


# --- split --------------------------------------------------------------------------------


def test_a_split_retires_the_parent_into_two_children(service_engine, user_id, i3302):
    e, u, w = service_engine, user_id, i3302
    _attempt(e, u, {"practice_item_id": w["sql_items"][0], "correct": True})
    parent_state = _row(e, topic_state, topic_id=w["mysql"])

    result = split_topic(e, u, w["mysql"], _two_children(w))

    parent = _row(e, topic, id=w["mysql"])
    assert (parent.status, parent.superseded_by) == ("superseded", None)
    kids = {c["name"]: c["topic_id"] for c in result["children"]}
    assert sorted(kids) == ["MySQL queries", "PHP database apps"]
    links = _rows(e, topic_split, parent_topic_id=w["mysql"])
    assert sorted(r.child_topic_id for r in links) == sorted(kids.values())
    for child_id in kids.values():
        child = _row(e, topic, id=child_id)
        assert (child.status, child.proposed_by) == ("active", "split")
        # D-193: the parent's numbers, marked as an estimate
        state = _row(e, topic_state, topic_id=child_id)
        assert state.inferred
        assert (state.stability, state.due_at, state.reps) == (
            parent_state.stability,
            parent_state.due_at,
            parent_state.reps,
        )
        # D-192: each child is examined in the Final
        edges = _rows(e, coverage, topic_id=child_id, assessment_id=w["final"])
        assert len(edges) == 1
    sql = {r.id for r in _rows(e, practice_item, topic_id=kids["MySQL queries"])}
    assert sql == set(w["sql_items"])
    # the attempt followed its item
    assert _row(e, attempt, practice_item_id=w["sql_items"][0]).topic_id == kids["MySQL queries"]
    assert _lineage(e, u) == []


def _listed(engine, uid, include_hidden):
    """MySQL's item ids as the host sees them in study_get_topics."""
    listing = get_topics(engine, uid, "I3302", include_hidden=include_hidden)
    [mysql] = [t for t in listing["topics"] if t["name"] == MYSQL]
    return [item["id"] for item in mysql["items"]], mysql["hidden"]


def test_a_split_after_a_right_answer_maps_from_the_full_listing(service_engine, user_id, i3302):
    """D-203: a mastered item is hidden from the default listing, so the host maps from
    include_hidden - the ids come from what the host sees, not from the fixture."""
    e, u, w = service_engine, user_id, i3302
    mastered = w["sql_items"][0]
    _attempt(e, u, {"practice_item_id": mastered, "correct": True})
    shown, hidden = _listed(e, u, include_hidden=False)
    assert mastered not in shown and hidden == 1

    every, hidden = _listed(e, u, include_hidden=True)
    assert set(every) == set(w["sql_items"] + w["page_items"]) and hidden == 0
    sql = [i for i in every if i in w["sql_items"]]
    pages = [i for i in every if i not in w["sql_items"]]
    result = split_topic(
        e,
        u,
        w["mysql"],
        [
            {"name": "MySQL queries", "item_ids": sql},
            {"name": "PHP database apps", "item_ids": pages},
        ],
    )

    queries = result["children"][0]["topic_id"]
    assert _row(e, practice_item, id=mastered).topic_id == queries
    assert _row(e, attempt, practice_item_id=mastered).topic_id == queries
    # nothing live is left on the retired parent to vote in a later derive (D-203)
    assert [r for r in _rows(e, practice_item, topic_id=w["mysql"]) if r.state == "live"] == []


def test_a_split_mapped_from_the_default_listing_names_the_way_out(service_engine, user_id, i3302):
    """The dead end QA found: the refusal must send the host to the listing that shows the id."""
    e, u, w = service_engine, user_id, i3302
    _attempt(e, u, {"practice_item_id": w["sql_items"][0], "correct": True})
    shown, _ = _listed(e, u, include_hidden=False)
    children = [
        {"name": "MySQL queries", "item_ids": [i for i in shown if i in w["sql_items"]]},
        {"name": "PHP database apps", "item_ids": [i for i in shown if i not in w["sql_items"]]},
    ]

    with pytest.raises(StudyError) as err:
        split_topic(e, u, w["mysql"], children)

    assert err.value.field_errors == [
        {"field": "children", "problem": f"missing: {w['sql_items'][0]}"}
    ]
    assert "include_hidden" in err.value.fix


@pytest.mark.parametrize("status", ["proposed", "superseded", "declined"])
def test_only_an_active_or_unexamined_topic_is_split(service_engine, user_id, i3302, status):
    with write_unit(service_engine) as conn:
        conn.execute(update(topic).where(topic.c.id == i3302["mysql"]).values(status=status))

    with pytest.raises(StudyError) as err:
        split_topic(service_engine, user_id, i3302["mysql"], _two_children(i3302))

    assert err.value.code == "not_fixable"
    assert _count(service_engine, topic_split) == 0


def test_one_child_is_refused(service_engine, user_id, i3302):
    one = [{"name": "MySQL", "item_ids": i3302["sql_items"] + i3302["page_items"]}]

    err = _refused(service_engine, user_id, i3302, one)

    assert err.field_errors == [
        {"field": "children", "problem": "a split needs 2 or more children"}
    ]


def test_every_map_problem_comes_back_in_one_error(service_engine, user_id, i3302):
    w = i3302
    children = [
        # page_items[1] missing; sql_items[0] twice; a session item is another topic's
        {"name": "MySQL queries", "item_ids": w["sql_items"] + [w["session_items"][0]]},
        {"name": "PHP database apps", "item_ids": [w["page_items"][0], w["sql_items"][0]]},
    ]

    err = _refused(service_engine, user_id, w, children)

    problems = " | ".join(f["problem"] for f in err.field_errors)
    assert f"missing: {w['page_items'][1]}" in problems
    assert f"listed twice: {w['sql_items'][0]}" in problems
    assert f"not this topic's item: {w['session_items'][0]}" in problems


def test_a_child_with_no_items_is_refused(service_engine, user_id, i3302):
    w = i3302
    children = [
        {"name": "MySQL queries", "item_ids": w["sql_items"] + w["page_items"]},
        {"name": "PHP database apps", "item_ids": []},
    ]

    err = _refused(service_engine, user_id, w, children)

    assert err.field_errors[0]["field"] == "children[1].item_ids"


@pytest.mark.parametrize("name", ["php sessions", MYSQL.upper()])
def test_a_child_named_like_a_live_topic_is_refused(service_engine, user_id, i3302, name):
    """Another topic's name, or the parent's own, in any case: that is a merge, not a split."""
    children = _two_children(i3302)
    children[1]["name"] = name

    err = _refused(service_engine, user_id, i3302, children)

    assert err.field_errors[0]["field"] == "children[1].name"


def test_two_children_with_one_name_are_refused(service_engine, user_id, i3302):
    children = _two_children(i3302)
    children[1]["name"] = "mysql QUERIES"

    err = _refused(service_engine, user_id, i3302, children)

    assert err.field_errors == [
        {"field": "children[1].name", "problem": "same name as children[0]"}
    ]


def test_a_name_held_by_a_proposal_asks_to_decide_it_not_to_merge(service_engine, user_id, i3302):
    """D-201: merge refuses a proposed topic, so the advice is to decide the proposal."""
    _topic(service_engine, user_id, i3302["course_id"], "PHP database apps", status="proposed")

    err = _refused(service_engine, user_id, i3302, _two_children(i3302))

    [problem] = [f["problem"] for f in err.field_errors]
    assert "study_confirm_topic_proposal" in problem
    assert "study_merge_topics" not in problem


def _generated(engine, uid, topic_id, n):
    """n generated practice items on `topic_id` - no paper asks them."""
    task_id = new_id()
    ids = [new_id() for _ in range(n)]
    with write_unit(engine) as conn:
        conn.execute(
            insert(generation_task).values(
                id=task_id,
                user_id=uid,
                kind="practice",
                scope_type="topic",
                scope_id=topic_id,
                route="host",
                created_at=now(),
            )
        )
        for k, item_id in enumerate(ids, start=1):
            conn.execute(
                insert(practice_item).values(
                    id=item_id,
                    user_id=uid,
                    topic_id=topic_id,
                    task_id=task_id,
                    item_ordinal=k,
                    origin="generated",
                    source_marker="model-knowledge",
                    question=f"generated {k}",
                    answer_provenance="none",
                    novelty_score=0.5,
                    created_at=now(),
                )
            )
    return ids


def test_an_unexamined_parent_gives_unexamined_children(service_engine, user_id, i3302):
    """D-201: no paper asks the children either, so they stay out of the denominator."""
    e, u = service_engine, user_id
    history = _topic(e, u, i3302["course_id"], "PHP history", status="unexamined")
    a, b = _generated(e, u, history, 2)
    children = [{"name": "PHP 4", "item_ids": [a]}, {"name": "PHP 5", "item_ids": [b]}]

    result = split_topic(e, u, history, children)

    statuses = {_row(e, topic, id=c["topic_id"]).status for c in result["children"]}
    assert statuses == {"unexamined"}


def test_a_child_holding_a_papers_item_is_active(service_engine, user_id, i3302):
    """D-204: MySQL's items are Final questions, so its children are examined."""
    with write_unit(service_engine) as conn:
        conn.execute(update(topic).where(topic.c.id == i3302["mysql"]).values(status="unexamined"))

    result = split_topic(service_engine, user_id, i3302["mysql"], _two_children(i3302))

    statuses = {_row(service_engine, topic, id=c["topic_id"]).status for c in result["children"]}
    assert statuses == {"active"}


def test_an_unexamined_target_handed_a_papers_item_is_active(service_engine, user_id, i3302):
    """D-204: sessions brings 2 Final questions, so the target's status follows."""
    e, u, w = service_engine, user_id, i3302
    intro = _topic(e, u, w["course_id"], "PHP intro", status="unexamined")

    merge_topics(e, u, w["sessions"], intro)

    assert _row(e, topic, id=intro).status == "active"
    assert len(_rows(e, coverage, topic_id=intro, assessment_id=w["final"])) == 1


def test_a_parent_never_practised_gives_its_children_no_state(service_engine, user_id, i3302):
    result = split_topic(service_engine, user_id, i3302["mysql"], _two_children(i3302))

    for child in result["children"]:
        assert _row(service_engine, topic_state, topic_id=child["topic_id"]) is None


def test_a_rejected_item_stays_on_the_parent_with_its_attempts(service_engine, user_id, i3302):
    """D-195: the map names live items only; the rest stays where it was."""
    e, u, w = service_engine, user_id, i3302
    [rejected] = _items(e, u, w["mysql"], _paper(e, u, _slot(e, w["final"]), "2019-09-06"), [10])
    _attempt(e, u, {"practice_item_id": rejected, "correct": False})
    with write_unit(e) as conn:
        conn.execute(
            update(practice_item).where(practice_item.c.id == rejected).values(state="rejected")
        )

    split_topic(e, u, w["mysql"], _two_children(w))

    assert _row(e, practice_item, id=rejected).topic_id == w["mysql"]
    assert _row(e, attempt, practice_item_id=rejected).topic_id == w["mysql"]


def _slot(engine, assessment_id):
    return _row(engine, assessment, id=assessment_id).slot_id


def _profile(engine, uid, assessment_id, version, weights):
    """A profile version of the assessment's slot, weighting {topic_id: weight}."""
    profile_id = new_id()
    slot_id = _slot(engine, assessment_id)  # read before the write unit locks the file
    with write_unit(engine) as conn:
        conn.execute(
            insert(exam_profile).values(
                id=profile_id,
                owner_id=uid,
                slot_id=slot_id,
                version=version,
                derived_at=now(),
                provisional_syllabus=False,
                provisional_instructor=True,
                provisional_format=True,
                evidence_count_syllabus=10,
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


def test_derive_again_names_the_slot_whose_latest_profile_weights_the_parent(
    service_engine, user_id, i3302
):
    w = i3302
    _profile(service_engine, user_id, w["final"], 6, {w["mysql"]: 60, w["sessions"]: 40})

    result = split_topic(service_engine, user_id, w["mysql"], _two_children(w))

    assert result["derive_again"] == [
        {
            "slot": "Final exam",
            "fix": "call study_derive_profile with code 'I3302' and assessment_name 'Final exam'",
        }
    ]


def test_an_older_profile_alone_asks_for_no_derive(service_engine, user_id, i3302):
    """Only the latest version is read by the plan; v2 without MySQL is already right."""
    w = i3302
    _profile(service_engine, user_id, w["final"], 1, {w["mysql"]: 100})
    _profile(service_engine, user_id, w["final"], 2, {w["sessions"]: 100})

    result = split_topic(service_engine, user_id, w["mysql"], _two_children(w))

    assert result["derive_again"] == []


def test_every_child_gets_the_parents_chapters_and_the_result_lists_them(
    service_engine, user_id, i3302
):
    """D-197: Chapter 6 was tagged to MySQL, so both children stay 'taught'."""
    w = i3302

    result = split_topic(service_engine, user_id, w["mysql"], _two_children(w))

    for child in result["children"]:
        assert (
            len(_rows(service_engine, coverage, topic_id=child["topic_id"], material_id=w["ch6"]))
            == 1
        )
    assert result["retag_materials"] == [
        {"material_id": w["ch6"], "filename": "PHP_Chapter6_Eng_DataBase.pdf"}
    ]
    # the parent keeps its own edge: nothing moved
    assert len(_rows(service_engine, coverage, topic_id=w["mysql"], material_id=w["ch6"])) == 1


# --- merge --------------------------------------------------------------------------------


def test_a_merge_retires_the_source_into_the_target(service_engine, user_id, i3302):
    e, u, w = service_engine, user_id, i3302
    _attempt(
        e,
        u,
        {"practice_item_id": w["session_items"][0], "correct": True},
        {"topic_id": w["sessions"], "correct": True},  # an item-less attempt
    )

    result = merge_topics(e, u, w["sessions"], w["cookies"])

    source = _row(e, topic, id=w["sessions"])
    assert (source.status, source.superseded_by) == ("superseded", w["cookies"])
    items = {r.id for r in _rows(e, practice_item, topic_id=w["cookies"])}
    assert items == set(w["cookie_items"]) | set(w["session_items"])
    assert _rows(e, attempt, topic_id=w["sessions"]) == []
    assert len(_rows(e, attempt, topic_id=w["cookies"])) == 2
    # combined: cookies had no attempt of its own, so its state is the replay of sessions'
    state = _row(e, topic_state, topic_id=w["cookies"])
    assert (state.reps, state.lapses, state.inferred) == (1, 0, False)
    assert result["target"]["state"]["reps"] == 1
    # the source's computed row is gone - its attempts left with its items (D-107)
    assert _row(e, topic_state, topic_id=w["sessions"]) is None
    assert _lineage(e, u) == []


def test_a_merge_combines_both_topics_days(service_engine, user_id, i3302, monkeypatch):
    """Both sides practised, on two days: the target's replay reads both topics' attempts, so
    its reps count both days (D-107, D-200). Noon UTC is one local day at any offset to +-11."""
    e, u, w = service_engine, user_id, i3302
    clock = {"now": "2026-10-06T12:00:00Z"}
    monkeypatch.setattr(study_loop, "now", lambda: clock["now"])
    _attempt(e, u, {"practice_item_id": w["session_items"][0], "correct": True})
    clock["now"] = "2026-10-08T12:00:00Z"
    _attempt(e, u, {"practice_item_id": w["cookie_items"][0], "correct": False})
    before = _row(e, topic_state, topic_id=w["cookies"])
    assert (before.reps, before.lapses) == (1, 1)  # cookies alone: one Again

    result = merge_topics(e, u, w["sessions"], w["cookies"])

    after = _row(e, topic_state, topic_id=w["cookies"])
    assert (after.reps, after.lapses, after.inferred) == (2, 1, False)  # Good on 6th, Again on 8th
    assert result["target"]["state"]["reps"] == 2


def test_a_merge_onto_an_existing_edge_writes_no_second_one(service_engine, user_id, i3302):
    """The collision D-192 is about: sessions -> Final and cookies -> Final both exist."""
    e, u, w = service_engine, user_id, i3302

    merge_topics(e, u, w["sessions"], w["cookies"])

    assert len(_rows(e, coverage, topic_id=w["cookies"], assessment_id=w["final"])) == 1
    assert len(_rows(e, coverage, topic_id=w["cookies"], assessment_id=w["partial"])) == 1
    assert len(_rows(e, coverage, topic_id=w["sessions"], assessment_id=w["final"])) == 1


def test_an_unpractised_merge_keeps_a_split_childs_estimate(service_engine, user_id, i3302):
    """D-200: no counted attempt moves, so the target is not replayed and its copy survives."""
    e, u, w = service_engine, user_id, i3302
    _attempt(e, u, {"practice_item_id": w["sql_items"][0], "correct": True})
    split = split_topic(e, u, w["mysql"], _two_children(w))
    apps = next(c["topic_id"] for c in split["children"] if c["name"] == "PHP database apps")
    before = _row(e, topic_state, topic_id=apps)  # the copy: apps holds no attempt of its own

    result = merge_topics(e, u, w["sessions"], apps)  # sessions was never practised

    after = _row(e, topic_state, topic_id=apps)
    assert after is not None and after.inferred
    assert (after.stability, after.due_at, after.reps) == (
        before.stability,
        before.due_at,
        before.reps,
    )
    assert result["target"]["state"]["reps"] == before.reps


def test_a_practised_merge_replaces_a_split_childs_estimate(service_engine, user_id, i3302):
    """D-200's other side: a counted attempt arrives, so the replay writes real evidence."""
    e, u, w = service_engine, user_id, i3302
    _attempt(e, u, {"practice_item_id": w["sql_items"][0], "correct": True})
    split = split_topic(e, u, w["mysql"], _two_children(w))
    apps = next(c["topic_id"] for c in split["children"] if c["name"] == "PHP database apps")
    _attempt(e, u, {"practice_item_id": w["session_items"][0], "correct": False})

    merge_topics(e, u, w["sessions"], apps)

    after = _row(e, topic_state, topic_id=apps)
    assert not after.inferred
    assert (after.reps, after.lapses) == (1, 1)  # sessions' one Again, apps' only evidence


def test_a_merge_moves_items_in_every_state(service_engine, user_id, i3302):
    e, u, w = service_engine, user_id, i3302
    with write_unit(e) as conn:
        conn.execute(
            update(practice_item)
            .where(practice_item.c.id == w["session_items"][1])
            .values(state="rejected")
        )

    merge_topics(e, u, w["sessions"], w["cookies"])

    assert _row(e, practice_item, id=w["session_items"][1]).topic_id == w["cookies"]


def test_a_merge_gives_the_target_the_sources_chapters(service_engine, user_id, i3302):
    e, u, w = service_engine, user_id, i3302

    result = merge_topics(e, u, w["mysql"], w["cookies"])

    assert len(_rows(e, coverage, topic_id=w["cookies"], material_id=w["ch6"])) == 1
    assert [m["material_id"] for m in result["retag_materials"]] == [w["ch6"]]


def test_merge_derive_again_names_the_final(service_engine, user_id, i3302):
    w = i3302
    _profile(service_engine, user_id, w["final"], 6, {w["sessions"]: 55, w["cookies"]: 45})

    result = merge_topics(service_engine, user_id, w["sessions"], w["cookies"])

    assert [d["slot"] for d in result["derive_again"]] == ["Final exam"]


def test_a_topic_cannot_merge_into_itself(service_engine, user_id, i3302):
    with pytest.raises(StudyError) as err:
        merge_topics(service_engine, user_id, i3302["sessions"], i3302["sessions"])

    assert err.value.field_errors[0]["field"] == "target_id"


def test_a_merge_across_courses_is_refused(service_engine, user_id, i3302):
    other = add_course(service_engine, user_id, "I3301", "Software Engineering", SEM)
    elsewhere = _topic(service_engine, user_id, other["course_id"], "Use case diagrams")

    with pytest.raises(StudyError) as err:
        merge_topics(service_engine, user_id, i3302["sessions"], elsewhere)

    assert err.value.field_errors[0]["problem"] == "another course's topic"
    assert _row(service_engine, topic, id=i3302["sessions"]).status == "active"


@pytest.mark.parametrize("side", ["source_id", "target_id"])
def test_a_retired_topic_takes_no_merge(service_engine, user_id, i3302, side):
    w = i3302
    with write_unit(service_engine) as conn:
        conn.execute(update(topic).where(topic.c.id == w["mysql"]).values(status="declined"))
    pair = {"source_id": w["sessions"], "target_id": w["cookies"], side: w["mysql"]}

    with pytest.raises(StudyError) as err:
        merge_topics(service_engine, user_id, pair["source_id"], pair["target_id"])

    assert err.value.code == "not_fixable"
    assert err.value.field_errors[0]["field"] == side


# --- move_items (his piece) ---------------------------------------------------------------


def test_moving_some_items_leaves_the_rest_and_the_itemless_attempts(
    service_engine, user_id, i3302
):
    e, u, w = service_engine, user_id, i3302
    _attempt(
        e,
        u,
        {"practice_item_id": w["sql_items"][0], "correct": True},
        {"practice_item_id": w["page_items"][0], "correct": False},
        {"topic_id": w["mysql"], "correct": True},
    )

    with write_unit(e) as conn:
        move_items(conn, u, w["mysql"], w["cookies"], [w["sql_items"][0]])

    assert _row(e, practice_item, id=w["sql_items"][0]).topic_id == w["cookies"]
    assert _row(e, practice_item, id=w["page_items"][0]).topic_id == w["mysql"]
    assert _row(e, attempt, practice_item_id=w["sql_items"][0]).topic_id == w["cookies"]
    assert _row(e, attempt, practice_item_id=w["page_items"][0]).topic_id == w["mysql"]
    itemless = _rows(e, attempt, practice_item_id=None)
    assert [a.topic_id for a in itemless] == [w["mysql"]]


def test_moving_all_takes_the_itemless_attempts_too(service_engine, user_id, i3302):
    e, u, w = service_engine, user_id, i3302
    _attempt(e, u, {"topic_id": w["sessions"], "correct": True})

    with write_unit(e) as conn:
        move_items(conn, u, w["sessions"], w["cookies"], None)

    assert [a.topic_id for a in _rows(e, attempt)] == [w["cookies"]]
    assert _rows(e, practice_item, topic_id=w["sessions"]) == []


def test_a_new_topic_gets_the_edges_of_the_exams_its_items_come_from(
    service_engine, user_id, i3302
):
    """A fresh topic with no edge: cookies' two items come from a Final and a Partial paper."""
    e, u, w = service_engine, user_id, i3302
    fresh = _topic(e, u, w["course_id"], "Fresh")

    with write_unit(e) as conn:
        move_items(conn, u, w["cookies"], fresh, w["cookie_items"])

    exams = {r.assessment_id for r in _rows(e, coverage, topic_id=fresh) if r.assessment_id}
    assert exams == {w["final"], w["partial"]}
    # nothing moved: cookies keeps both of its own
    assert len([r for r in _rows(e, coverage, topic_id=w["cookies"]) if r.assessment_id]) == 2


# --- lineage_violations (his piece) -------------------------------------------------------


def _retire(engine, topic_id, superseded_by=None, children=()):
    """Write a lineage by hand - the bad shapes no tool writes."""
    owner = _row(engine, topic, id=topic_id).user_id
    with write_unit(engine) as conn:
        conn.execute(
            update(topic)
            .where(topic.c.id == topic_id)
            .values(status="superseded", superseded_by=superseded_by)
        )
        for child in children:
            conn.execute(
                insert(topic_split).values(
                    id=new_id(),
                    user_id=owner,
                    parent_topic_id=topic_id,
                    child_topic_id=child,
                    created_at=now(),
                )
            )


def test_healthy_lineages_report_nothing(service_engine, user_id, i3302):
    w = i3302
    _retire(service_engine, w["sessions"], superseded_by=w["cookies"])
    _retire(service_engine, w["mysql"], children=[w["cookies"], w["sessions"]])

    assert _lineage(service_engine, user_id) == []


def test_a_superseded_topic_with_both_lineages_is_reported(service_engine, user_id, i3302):
    w = i3302
    _retire(
        service_engine,
        w["mysql"],
        superseded_by=w["cookies"],
        children=[w["cookies"], w["sessions"]],
    )

    assert _lineage(service_engine, user_id) == [w["mysql"]]


def test_a_superseded_topic_with_neither_lineage_is_reported(service_engine, user_id, i3302):
    _retire(service_engine, i3302["mysql"])

    assert _lineage(service_engine, user_id) == [i3302["mysql"]]


def test_a_split_with_one_child_is_reported(service_engine, user_id, i3302):
    _retire(service_engine, i3302["mysql"], children=[i3302["cookies"]])

    assert _lineage(service_engine, user_id) == [i3302["mysql"]]


def test_live_topics_and_other_users_are_not_this_checks_business(
    service_engine, user_id, other_user_id, i3302
):
    _retire(service_engine, i3302["mysql"])

    assert _lineage(service_engine, other_user_id) == []
    # three active topics, no lineage: not reported
    with write_unit(service_engine) as conn:
        conn.execute(update(topic).where(topic.c.id == i3302["mysql"]).values(status="active"))
    assert _lineage(service_engine, user_id) == []


# --- the tools ----------------------------------------------------------------------------


def test_both_tools_are_listed(service_engine):
    from studysystem.server import create_server

    names = tool_names(create_server(service_engine))

    assert {"study_split_topic", "study_merge_topics"} <= names


def test_a_refused_merge_reaches_the_host_as_an_error_result(call, i3302):
    result = call(
        "study_merge_topics", {"source_id": i3302["sessions"], "target_id": i3302["sessions"]}
    )

    assert result.is_error
    assert json.loads(result.content[0].text)["code"] == "invalid_value"


def test_the_listing_tool_takes_include_hidden(call, service_engine, user_id, i3302):
    _attempt(service_engine, user_id, {"practice_item_id": i3302["sql_items"][0], "correct": True})

    result = call("study_get_topics", {"code": "I3302", "include_hidden": True})

    [mysql] = [t for t in json.loads(result.content[0].text)["topics"] if t["name"] == MYSQL]
    assert len(mysql["items"]) == 4 and mysql["hidden"] == 0


def test_a_split_through_the_tool(call, i3302):
    result = call(
        "study_split_topic", {"topic_id": i3302["mysql"], "children": _two_children(i3302)}
    )

    assert not result.is_error
    assert len(json.loads(result.content[0].text)["children"]) == 2
