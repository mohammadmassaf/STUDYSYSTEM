"""Rescope (4.4; D-16 d, D-261, D-264) - on both engines.

Each test follows I3301's 2022-04-13 scan: its transcription closed on 2026-10-20 with the
question under page 2's diagram dropped, so the set is retired and the paper read again."""

import pytest
from sqlalchemy import insert, select, update

from studysystem.db.tables import (
    coverage,
    generation_task,
    item_rejection,
    past_exam,
    practice_item,
    replacement_queue,
    topic,
)
from studysystem.errors import StudyError
from studysystem.services.courses import add_course
from studysystem.services.generation import confirm_transcription, start_generation
from studysystem.services.ids import new_id, now
from studysystem.services.materials import add_material
from studysystem.services.rescope import _retire_edges, _supersede, rescope_generation
from studysystem.services.topics import edge_if_missing
from studysystem.services.units import write_unit
from tests._papers import pdf_pages, write
from tests.test_scan_transcription import item, register, submit, the_paper
from tests.test_study_loop_services import _paper, _topic

SEM = "Semester 1 2026-2027"
CLOSED = "2026-10-20T17:12:38Z"


def _task(engine, uid, scope_id, *, kind="transcription", scope_type="past-exam", closed=CLOSED):
    """A generation task; `closed=None` leaves it open."""
    task_id = new_id()
    status = {} if closed is None else {"status": "closed", "closed_at": closed}
    with write_unit(engine) as conn:
        conn.execute(
            insert(generation_task).values(
                id=task_id,
                user_id=uid,
                kind=kind,
                scope_type=scope_type,
                scope_id=scope_id,
                route="host",
                created_at=now(),
                **status,
            )
        )
    return task_id


def _questions(engine, uid, task_id, paper_id, placed):
    """The paper's questions on `task_id`: `placed` is [(position, topic_id), ...]."""
    ids = []
    with write_unit(engine) as conn:
        for position, topic_id in placed:
            item_id = new_id()
            conn.execute(
                insert(practice_item).values(
                    id=item_id,
                    user_id=uid,
                    topic_id=topic_id,
                    task_id=task_id,
                    item_ordinal=position,
                    origin="past-exam",
                    past_exam_id=paper_id,
                    position=position,
                    page=1,
                    question=f"question {position}",
                    answer_provenance="none",
                    source_marker="from-material",
                    created_at=now(),
                )
            )
            ids.append(item_id)
    return ids


@pytest.fixture
def scan(service_engine, user_id):
    """I3301's scan: three topics, its closed transcription with five questions, and the new task
    a rescope opens on the same paper. Returns a dict of ids."""
    course = add_course(service_engine, user_id, "I3301", "Software Engineering", SEM)
    testing = _topic(service_engine, user_id, course["course_id"], "Software testing")
    classes = _topic(service_engine, user_id, course["course_id"], "Class diagram")
    uml = _topic(service_engine, user_id, course["course_id"], "UML fundamentals")
    paper = _paper(service_engine, user_id, course["slot_id"])
    old = _task(service_engine, user_id, paper)
    placed = [(1, testing), (2, uml), (3, testing), (4, classes), (5, classes)]
    items = _questions(service_engine, user_id, old, paper, placed)
    new = _task(service_engine, user_id, paper, closed=None)
    return {
        "paper": paper,
        "old": old,
        "new": new,
        "items": items,
        "topics": {testing, classes, uml},
    }


def _run(engine, task_id, new_task_id):
    with write_unit(engine) as conn:
        old = conn.execute(select(generation_task).where(generation_task.c.id == task_id)).one()
        return _supersede(conn, old, new_task_id)


def _task_row(engine, task_id):
    with engine.connect() as conn:
        return conn.execute(select(generation_task).where(generation_task.c.id == task_id)).one()


def _states(engine, item_ids):
    with engine.connect() as conn:
        rows = conn.execute(
            select(practice_item.c.id, practice_item.c.state).where(
                practice_item.c.id.in_(item_ids)
            )
        )
        return {row.id: row.state for row in rows}


def test_a_closed_scan_and_all_its_items_are_superseded(service_engine, scan):
    touched = _run(service_engine, scan["old"], scan["new"])

    old = _task_row(service_engine, scan["old"])
    assert (old.status, old.superseded_by, old.closed_at) == ("superseded", scan["new"], CLOSED)
    assert set(_states(service_engine, scan["items"]).values()) == {"superseded"}
    assert touched == scan["topics"]


def test_an_open_task_gets_a_close_time(service_engine, user_id, scan):
    material_id = new_id()  # the scope is never looked up here
    opened = _task(
        service_engine, user_id, material_id, kind="practice", scope_type="material", closed=None
    )

    touched = _run(service_engine, opened, scan["new"])

    row = _task_row(service_engine, opened)
    assert (row.status, row.superseded_by) == ("superseded", scan["new"])
    assert row.closed_at is not None
    assert touched == set()


def test_a_task_with_no_item_returns_no_topic(service_engine, user_id, scan):
    empty = _task(service_engine, user_id, scan["paper"])

    assert _run(service_engine, empty, scan["new"]) == set()
    assert _task_row(service_engine, empty).status == "superseded"


def test_a_rejected_item_is_superseded_and_keeps_its_rejection(service_engine, user_id, scan):
    rejected = scan["items"][1]
    rejection_id = new_id()
    with write_unit(service_engine) as conn:
        conn.execute(
            update(practice_item).where(practice_item.c.id == rejected).values(state="rejected")
        )
        conn.execute(
            insert(item_rejection).values(
                id=rejection_id,
                user_id=user_id,
                practice_item_id=rejected,
                reason="off-syllabus",
                created_at=now(),
            )
        )

    _run(service_engine, scan["old"], scan["new"])

    assert _states(service_engine, [rejected]) == {rejected: "superseded"}
    with service_engine.connect() as conn:
        kept = conn.execute(select(item_rejection)).one()
    assert (kept.id, kept.practice_item_id, kept.reason) == (rejection_id, rejected, "off-syllabus")


def test_another_task_on_the_same_paper_is_untouched(service_engine, user_id, scan):
    earlier = _task(service_engine, user_id, scan["paper"])
    some_topic = next(iter(scan["topics"]))
    [kept] = _questions(service_engine, user_id, earlier, scan["paper"], [(6, some_topic)])

    _run(service_engine, scan["old"], scan["new"])

    assert _states(service_engine, [kept]) == {kept: "live"}
    assert _task_row(service_engine, earlier).status == "closed"


# --- _retire_edges ---------------------------------------------------------

EARLIER = "2026-11-01T09:00:00Z"


def _second_paper(engine, uid, slot_id):
    """Another I3301 Final paper - its own file, so its own hash."""
    paper_id = new_id()
    with write_unit(engine) as conn:
        conn.execute(
            insert(past_exam).values(
                id=paper_id,
                owner_id=uid,
                slot_id=slot_id,
                session_type="first",
                session_date="2023-03-16",
                instructor_tier="unknown",
                file_ref="files/y",
                content_sha256="b" * 64,
                created_at=now(),
            )
        )
    return paper_id


def _edge(engine, uid, topic_id, assessment_id, **extra):
    """One topic -> exam edge, `inferred` and unconfirmed unless `extra` says otherwise."""
    edge_id = new_id()
    row = {"tier": "inferred", **extra}
    with write_unit(engine) as conn:
        conn.execute(
            insert(coverage).values(
                id=edge_id,
                user_id=uid,
                topic_id=topic_id,
                assessment_id=assessment_id,
                created_at=now(),
                **row,
            )
        )
    return edge_id


@pytest.fixture
def edges(service_engine, user_id):
    """I3301's scan after `_supersede`: six topics, each with one question on the scan (now
    superseded) and one edge to I3301 Final. Software testing also has a live question on a
    second Final paper. Returns {"topics": {...}, "edges": {name: edge id}}."""
    course = add_course(service_engine, user_id, "I3301", "Software Engineering", SEM)
    final = course["assessment_id"]
    names = ["uml", "testing", "confirmed", "declared", "syllabus", "retired"]
    ids = {n: _topic(service_engine, user_id, course["course_id"], n) for n in names}
    with write_unit(service_engine) as conn:
        conn.execute(
            update(topic).where(topic.c.id == ids["syllabus"]).values(proposed_by="syllabus")
        )

    scan_paper = _paper(service_engine, user_id, course["slot_id"])
    old = _task(service_engine, user_id, scan_paper)
    placed = [(n, ids[name]) for n, name in enumerate(names, start=1)]
    _questions(service_engine, user_id, old, scan_paper, placed)
    with write_unit(service_engine) as conn:
        conn.execute(
            update(practice_item).where(practice_item.c.task_id == old).values(state="superseded")
        )

    other_paper = _second_paper(service_engine, user_id, course["slot_id"])
    other = _task(service_engine, user_id, other_paper)
    _questions(service_engine, user_id, other, other_paper, [(1, ids["testing"])])

    extra = {
        "confirmed": {"confirmed_at": EARLIER},
        "declared": {"tier": "declared"},
        "retired": {"retired_at": EARLIER},
    }
    made = {n: _edge(service_engine, user_id, ids[n], final, **extra.get(n, {})) for n in names}
    return {"topics": set(ids.values()), "edges": made}


def _retire(engine, topic_ids):
    with write_unit(engine) as conn:
        return _retire_edges(conn, topic_ids)


def _retired_at(engine, edge_id):
    with engine.connect() as conn:
        return conn.execute(
            select(coverage.c.retired_at).where(coverage.c.id == edge_id)
        ).scalar_one()


def test_an_edge_its_only_question_backed_is_retired(service_engine, edges):
    retired = _retire(service_engine, edges["topics"])

    assert retired == [edges["edges"]["uml"]]
    assert _retired_at(service_engine, edges["edges"]["uml"]) is not None


def test_an_edge_another_paper_still_backs_stays(service_engine, edges):
    _retire(service_engine, edges["topics"])

    assert _retired_at(service_engine, edges["edges"]["testing"]) is None


def test_a_confirmed_edge_stays(service_engine, edges):
    _retire(service_engine, edges["topics"])

    assert _retired_at(service_engine, edges["edges"]["confirmed"]) is None


def test_a_declared_edge_stays(service_engine, edges):
    _retire(service_engine, edges["topics"])

    assert _retired_at(service_engine, edges["edges"]["declared"]) is None


def test_an_edge_on_a_syllabus_topic_stays(service_engine, edges):
    _retire(service_engine, edges["topics"])

    assert _retired_at(service_engine, edges["edges"]["syllabus"]) is None


def test_an_edge_already_retired_keeps_its_first_time(service_engine, edges):
    retired = _retire(service_engine, edges["topics"])

    assert edges["edges"]["retired"] not in retired
    assert _retired_at(service_engine, edges["edges"]["retired"]) == EARLIER


# --- rescope_generation: refusals -------------------------------------------


def _refused(call, *args, **kwargs) -> StudyError:
    with pytest.raises(StudyError) as excinfo:
        call(*args, **kwargs)
    return excinfo.value


def test_an_unknown_task_is_not_found(service_engine, user_id):
    err = _refused(rescope_generation, service_engine, user_id, new_id())

    assert err.code == "not_found"


def test_a_task_already_rescoped_is_refused(service_engine, user_id):
    practice = _task(service_engine, user_id, new_id(), kind="practice", scope_type="material")
    rescope_generation(service_engine, user_id, practice)

    err = _refused(rescope_generation, service_engine, user_id, practice)

    assert err.code == "task_closed"


def test_a_note_task_is_sent_to_redo(service_engine, user_id):
    note_task = _task(service_engine, user_id, new_id(), kind="note", scope_type="material")

    err = _refused(rescope_generation, service_engine, user_id, note_task)

    assert err.code == "not_supported"
    assert "redo=true" in err.fix


def test_a_replacement_task_is_sent_to_feedback(service_engine, user_id):
    topic_task = _task(service_engine, user_id, new_id(), kind="practice", scope_type="topic")

    err = _refused(rescope_generation, service_engine, user_id, topic_task)

    assert err.code == "not_supported"
    assert "study_submit_feedback" in err.fix


def test_a_transcription_takes_no_new_scope(service_engine, user_id, scan):
    err = _refused(rescope_generation, service_engine, user_id, scan["old"], count=12)

    assert (err.code, err.field_errors[0]["field"]) == ("invalid_value", "count")


def test_a_scope_with_another_open_task_is_refused(service_engine, user_id, scan):
    # the fixture's open task on the paper stands for a redo already under way
    err = _refused(rescope_generation, service_engine, user_id, scan["old"])

    assert err.code == "task_open"
    assert scan["new"] in err.message


# --- rescope_generation: a wrong scan, redone end to end ---------------------


@pytest.fixture
def wrong_scan(service_engine, user_id, tmp_path):
    """I3301 2022-04-13 as a three-page camera scan, transcribed with page 3's question
    dropped: two items, the task closed, the paper not confirmed. Returns (paper, task)."""
    add_course(service_engine, user_id, "I3301", "Software Engineering", SEM)
    paper = register(service_engine, user_id, tmp_path, "scan", [None] * 3, pictured={1, 2, 3})
    task = start_generation(service_engine, user_id, "transcription", paper).payload["task_id"]
    reply = submit(service_engine, user_id, task, [item(1, 1), item(2, 2)])
    assert reply["status"] == "closed"
    return paper, task


def test_a_wrong_scan_is_retired_and_its_paper_counts_nowhere(service_engine, user_id, wrong_scan):
    paper, task = wrong_scan

    reply = rescope_generation(service_engine, user_id, task)

    assert (reply["kind"], reply["scope_id"], reply["superseded_items"]) == (
        "transcription",
        paper,
        2,
    )
    old = _task_row(service_engine, task)
    assert (old.status, old.superseded_by) == ("superseded", reply["new_task_id"])
    new = _task_row(service_engine, reply["new_task_id"])
    assert (new.status, new.kind, new.scope_id) == ("open", "transcription", paper)
    row = the_paper(service_engine, paper)
    assert (row.transcript_task_id, row.transcript_confirmed_at) == (None, None)


def test_the_redo_is_fetched_and_writes_the_same_positions_again(
    service_engine, user_id, wrong_scan
):
    paper, task = wrong_scan
    new_task_id = rescope_generation(service_engine, user_id, task)["new_task_id"]

    fetched = start_generation(service_engine, user_id, "transcription", paper)
    reply = submit(service_engine, user_id, new_task_id)  # all three questions this time

    assert fetched.payload["task_id"] == new_task_id
    assert (reply["status"], reply["accepted"]) == ("closed", [1, 2, 3])
    assert the_paper(service_engine, paper).transcript_task_id == new_task_id
    confirm_transcription(service_engine, user_id, paper)  # and he can confirm it


# --- edge_if_missing brings a retired edge back ------------------------------


def test_a_question_landing_again_brings_the_retired_edge_back(service_engine, edges):
    retired = edges["edges"]["retired"]
    with service_engine.connect() as conn:
        row = conn.execute(select(coverage).where(coverage.c.id == retired)).one()

    with write_unit(service_engine) as conn:
        edge_if_missing(conn, row.user_id, row.topic_id, row.assessment_id)

    with service_engine.connect() as conn:
        rows = conn.execute(
            select(coverage.c.id, coverage.c.retired_at).where(coverage.c.topic_id == row.topic_id)
        ).all()
    assert [(r.id, r.retired_at) for r in rows] == [(retired, None)]  # same row, live


# --- rescope_generation: a practice set on the wrong chapter -----------------


def test_a_practice_set_moves_to_the_right_chapter_with_its_count(
    service_engine, user_id, tmp_path
):
    add_course(service_engine, user_id, "I3304", "Computer Networks", SEM)
    chapters = []
    for n in (1, 2):
        path = write(tmp_path, f"chapter {n}.pdf", pdf_pages([f"Chapter {n}"]))
        chapters.append(
            add_material(service_engine, user_id, "I3304", path, "chapter")["material_id"]
        )
    wrong = _task(service_engine, user_id, chapters[0], kind="practice", scope_type="material")
    with write_unit(service_engine) as conn:
        conn.execute(
            update(generation_task).where(generation_task.c.id == wrong).values(item_count=10)
        )

    moved = rescope_generation(service_engine, user_id, wrong, material_id=chapters[1])
    recounted = rescope_generation(service_engine, user_id, moved["new_task_id"], count=5)

    assert (moved["scope_id"], moved["item_count"]) == (chapters[1], 10)  # count copied
    assert (recounted["scope_id"], recounted["item_count"]) == (chapters[1], 5)
    assert recounted["retired_edges"] == 0  # a practice rescope never touches an exam edge


def _chapters(engine, uid, tmp_path, code, n):
    """`n` chapter PDFs of course `code`, registered as materials. Returns their ids."""
    ids = []
    for k in range(1, n + 1):
        path = write(tmp_path, f"chapter {k}.pdf", pdf_pages([f"Chapter {k}"]))
        ids.append(add_material(engine, uid, code, path, "chapter")["material_id"])
    return ids


def test_a_rescoped_practice_task_is_fetched_on_its_new_chapter(service_engine, user_id, tmp_path):
    course = add_course(service_engine, user_id, "I3304", "Computer Networks", SEM)
    _topic(service_engine, user_id, course["course_id"], "TCP vs UDP")
    chapter_1, chapter_2 = _chapters(service_engine, user_id, tmp_path, "I3304", 2)
    started = start_generation(
        service_engine, user_id, "practice", material_id=chapter_1, count=10
    ).payload["task_id"]

    new_task_id = rescope_generation(service_engine, user_id, started, material_id=chapter_2)[
        "new_task_id"
    ]
    fetched = start_generation(service_engine, user_id, "practice", material_id=chapter_2)

    assert fetched.payload["task_id"] == new_task_id
    row = _task_row(service_engine, new_task_id)
    assert (row.scope_id, row.item_count, row.parts_sent) == (chapter_2, 10, 1)
    assert _task_row(service_engine, started).status == "superseded"


# --- rescope_generation drops the holes its old task left (D-265) -------------


def _generated(engine, uid, task_id, topic_id):
    item_id = new_id()
    with write_unit(engine) as conn:
        conn.execute(
            insert(practice_item).values(
                id=item_id,
                user_id=uid,
                topic_id=topic_id,
                task_id=task_id,
                item_ordinal=1,
                origin="generated",
                question="a generated question",
                answer_provenance="none",
                source_marker="from-material",
                novelty_score=0.5,
                created_at=now(),
            )
        )
    return item_id


def _hole(engine, uid, topic_id, **source):
    """One pending replacement row; `source` is closed_task_id or rejection_id."""
    row_id = new_id()
    with write_unit(engine) as conn:
        conn.execute(
            insert(replacement_queue).values(
                id=row_id, user_id=uid, topic_id=topic_id, created_at=now(), **source
            )
        )
    return row_id


def test_a_rescope_drops_the_holes_its_old_task_left(service_engine, user_id, tmp_path):
    # I3304 practice from chapter 1 by mistake: it closed partial with a hole, and one of its
    # items was rejected. Another set's hole on the same topic is not its to drop.
    course = add_course(service_engine, user_id, "I3304", "Computer Networks", SEM)
    tcp = _topic(service_engine, user_id, course["course_id"], "TCP vs UDP")
    chapter_1, chapter_2 = _chapters(service_engine, user_id, tmp_path, "I3304", 2)
    wrong = _task(service_engine, user_id, chapter_1, kind="practice", scope_type="material")
    other = _task(service_engine, user_id, chapter_2, kind="practice", scope_type="material")
    with write_unit(service_engine) as conn:
        conn.execute(
            update(generation_task).where(generation_task.c.id == wrong).values(status="partial")
        )
    item_id = _generated(service_engine, user_id, wrong, tcp)
    rejection_id = new_id()
    with write_unit(service_engine) as conn:
        conn.execute(
            insert(item_rejection).values(
                id=rejection_id,
                user_id=user_id,
                practice_item_id=item_id,
                reason="unclear",
                created_at=now(),
            )
        )
    from_partial = _hole(service_engine, user_id, tcp, closed_task_id=wrong)
    from_rejection = _hole(service_engine, user_id, tcp, rejection_id=rejection_id)
    not_ours = _hole(service_engine, user_id, tcp, closed_task_id=other)

    reply = rescope_generation(service_engine, user_id, wrong, material_id=chapter_2)

    assert reply["dropped_replacements"] == 2
    with service_engine.connect() as conn:
        rows = conn.execute(
            select(
                replacement_queue.c.id,
                replacement_queue.c.status,
                replacement_queue.c.resolved_at,
            )
        ).all()
    status = {row.id: row.status for row in rows}
    assert status == {from_partial: "dropped", from_rejection: "dropped", not_ours: "pending"}
    assert all((row.resolved_at is None) == (row.status == "pending") for row in rows)
