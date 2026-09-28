"""Past exams (D-42, D-43, D-44): I3302's three papers go into its existing `Final exam` slot, the
file is kept by hash, and every refusal writes nothing - no row, no copy."""

import json

import pytest
from sqlalchemy import func, select

from studysystem.db.tables import past_exam
from studysystem.errors import StudyError
from studysystem.services import intake
from studysystem.services.assessments import add_assessment
from studysystem.services.courses import add_course
from studysystem.services.past_exams import add_past_exam
from tests._papers import JPEG, pdf_bytes, write

SEM = "Semester 1 2026-2027"
HAMZE = "Dr. Mohamad Hamze"


@pytest.fixture
def web(service_engine, user_id):
    """I3302 with its default Final exam and a Partial exam."""
    ids = add_course(service_engine, user_id, "I3302", "Server-Side Web Development", SEM)
    partial = add_assessment(service_engine, user_id, "I3302", "Partial exam", "exam", 30)
    return {**ids, "partial_slot_id": partial["slot_id"]}


@pytest.fixture
def papers(tmp_path):
    """I3302's three papers as the host would point at them - each a PDF with its own text."""
    folder = tmp_path / "past-exams"
    return {
        "2020-S1": write(folder, "I3302_20192020_First.pdf", pdf_bytes("Final 2020-02-17")),
        "2020-S2": write(folder, "I3302_20192020_Second.pdf", pdf_bytes("Session 2 2020-09-14")),
        "2021-S2": write(folder, "I3302_20202021_Second.pdf", pdf_bytes("Session 2 2021-09-20")),
    }


def add(engine, user_id, paths, **overrides):
    """The first-session paper's facts, with any of them overridden."""
    args = {
        "code": "I3302",
        "assessment_name": "Final exam",
        "session_type": "first",
        "session_date": "2020-02-17",
        "instructor": HAMZE,
    } | overrides
    return add_past_exam(engine, user_id, paths=paths, **args)


def rows(engine):
    with engine.connect() as conn:
        return [r._mapping for r in conn.execute(select(past_exam).order_by("session_date"))]


def count(engine) -> int:
    with engine.connect() as conn:
        return conn.execute(select(func.count()).select_from(past_exam)).scalar_one()


def nothing_kept() -> bool:
    return not intake.papers_dir().exists() or not any(intake.papers_dir().iterdir())


# --- registering ------------------------------------------------------------


def test_the_first_session_paper_is_registered_under_the_final(
    service_engine, user_id, web, papers
):
    result = add(service_engine, user_id, papers["2020-S1"])

    (row,) = rows(service_engine)
    assert set(result) == {"past_exam_id", "slot_id", "file_ref", "has_text_layer", "pages"}
    assert result["past_exam_id"] == row["id"] and result["slot_id"] == web["slot_id"]
    assert (row["owner_id"], row["slot_id"]) == (user_id, web["slot_id"])
    assert (row["session_type"], row["session_date"], row["session_delayed"]) == (
        "first",
        "2020-02-17",
        0,
    )
    assert (row["instructor"], row["instructor_tier"]) == (HAMZE, "declared")
    assert row["has_text_layer"] == 1 and result["has_text_layer"] is True
    assert len(row["content_sha256"]) == 64
    assert row["file_ref"] == result["file_ref"] == f"{row['content_sha256']}.pdf"
    assert row["transcript_task_id"] is None
    kept = intake.papers_dir() / row["file_ref"]
    assert kept.read_bytes() == pdf_bytes("Final 2020-02-17")  # the server's copy, not the path
    assert "past-exams" not in json.dumps(result)  # the path never comes back (D-44)


def test_i3302_s_three_papers_share_one_pool(service_engine, user_id, web, papers):
    """The resits go under the same slot as session 'second' - one pool, three papers (D-14)."""
    add(service_engine, user_id, papers["2020-S1"])
    add(
        service_engine,
        user_id,
        papers["2020-S2"],
        session_type="second",
        session_date="2020-09-14",
    )
    add(
        service_engine,
        user_id,
        papers["2021-S2"],
        session_type="second",
        session_date="2021-09-20",
    )

    assert [(r["slot_id"], r["session_type"], r["session_date"]) for r in rows(service_engine)] == [
        (web["slot_id"], "first", "2020-02-17"),
        (web["slot_id"], "second", "2020-09-14"),
        (web["slot_id"], "second", "2021-09-20"),
    ]


def test_no_instructor_is_unknown_never_a_guess(service_engine, user_id, web, papers):
    add(service_engine, user_id, papers["2020-S1"], instructor=None)

    (row,) = rows(service_engine)
    assert (row["instructor"], row["instructor_tier"]) == (None, "unknown")


def test_the_slot_and_session_type_match_in_any_case(service_engine, user_id, web, papers):
    add(
        service_engine,
        user_id,
        papers["2020-S1"],
        assessment_name="final EXAM",
        session_type=" First ",
    )

    (row,) = rows(service_engine)
    assert (row["slot_id"], row["session_type"]) == (web["slot_id"], "first")


def test_a_delayed_session_is_recorded(service_engine, user_id, web, papers):
    add(service_engine, user_id, papers["2020-S1"], session_delayed="true")

    assert rows(service_engine)[0]["session_delayed"] == 1


def test_a_paper_in_photos_is_one_row_with_its_pages(service_engine, user_id, web, tmp_path):
    photos = [write(tmp_path / "dl", f"IMG_{i}.jpg", JPEG + bytes([i])) for i in (1, 2, 3, 4)]

    result = add(service_engine, user_id, photos)

    (row,) = rows(service_engine)
    assert row["has_text_layer"] == 0 and row["file_ref"].endswith("/")
    assert result["pages"] == ["01.jpg", "02.jpg", "03.jpg", "04.jpg"]


def test_the_same_pdf_under_another_slot_is_its_own_row(service_engine, user_id, web, papers):
    """The unique rule is (slot, hash): one file can be evidence for two components."""
    add(service_engine, user_id, papers["2020-S1"])
    add(service_engine, user_id, papers["2020-S1"], assessment_name="Partial exam")

    assert {r["slot_id"] for r in rows(service_engine)} == {web["slot_id"], web["partial_slot_id"]}
    assert len(list(intake.papers_dir().iterdir())) == 1  # one copy serves both


# --- refusals: each writes nothing -------------------------------------------


def refused(engine, user_id, paths, **overrides) -> StudyError:
    with pytest.raises(StudyError) as excinfo:
        add(engine, user_id, paths, **overrides)
    return excinfo.value


def test_a_near_miss_slot_is_not_found_and_nothing_is_kept(service_engine, user_id, web, papers):
    """D-42: "Final" makes no second slot - the pool would split."""
    err = refused(service_engine, user_id, papers["2020-S1"], assessment_name="Final")

    assert err.code == "not_found"
    assert "Final exam" in err.fix and "Partial exam" in err.fix
    assert count(service_engine) == 0 and nothing_kept()


def test_the_same_paper_twice_under_one_slot_is_refused(service_engine, user_id, web, papers):
    add(service_engine, user_id, papers["2020-S1"])

    err = refused(service_engine, user_id, papers["2020-S1"])

    assert err.code == "past_exam_exists"
    assert err.fix
    assert count(service_engine) == 1


def test_the_same_photos_in_another_order_are_the_same_paper(
    service_engine, user_id, web, tmp_path
):
    p1, p2, p3 = (write(tmp_path / "dl", f"IMG_{i}.jpg", JPEG + bytes([i])) for i in (1, 2, 3))
    add(service_engine, user_id, [p1, p2, p3])

    assert refused(service_engine, user_id, [p3, p1, p2]).code == "past_exam_exists"
    assert count(service_engine) == 1


@pytest.mark.parametrize(
    ("override", "field"),
    [
        ({"session_date": "2020-02"}, "session_date"),  # D-43: exact only
        ({"session_date": "~2020-02-17"}, "session_date"),
        ({"session_date": "17/02/2020"}, "session_date"),
        ({"session_type": "resit"}, "session_type"),
        ({"instructor": "  "}, "instructor"),
        ({"session_delayed": "yes"}, "session_delayed"),
    ],
)
def test_a_bad_value_is_invalid_and_writes_nothing(
    service_engine, user_id, web, papers, override, field
):
    err = refused(service_engine, user_id, papers["2020-S1"], **override)

    assert err.code == "invalid_value"
    assert err.field_errors[0]["field"] == field
    assert count(service_engine) == 0 and nothing_kept()


def test_a_missing_file_is_invalid_and_writes_nothing(service_engine, user_id, web, tmp_path):
    err = refused(service_engine, user_id, str(tmp_path / "nope.pdf"))

    assert (err.code, err.field_errors[0]["field"]) == ("invalid_value", "paths")
    assert count(service_engine) == 0
