"""Chapter summaries (1.20, D-131, D-133): Chapter 6 leaves as its pages, what the exams ask of
it and the summary rules, read-only; the host's text comes back and lands in the course's
`summaries/` as `_summary.md` with its PDF, never over a file. No row is written anywhere - nothing
else reads a summary.

Every test writes to a vault under tmp_path; conftest unsets the real one."""

import json

import pytest
from sqlalchemy import func, insert, select, update

from studysystem.db.tables import (
    assessment_slot,
    coverage,
    exam_profile,
    generation_task,
    metadata,
    note,
    practice_item,
    topic,
    topic_weight,
)
from studysystem.errors import StudyError
from studysystem.server import create_server
from studysystem.services.courses import set_course_input
from studysystem.services.ids import new_id, now
from studysystem.services.summaries import SUMMARY_STEPS, save_summary, summary_task
from studysystem.services.units import write_unit
from tests._client import tool_names
from tests.test_note_generation import CH6_PAGES, i3302, material, web  # noqa: F401 - fixtures
from tests.test_profiles import NAMES

FOLDER = "uni/Semester 1 2026-2027/Server-Side Web Development"
MD_FILE = "PHP_Chapter6_Eng_DataBase_summary.md"
PDF_FILE = "PHP_Chapter6_Eng_DataBase_summary.pdf"
SUMMARY = "# PHP & MySQL\n\n## Connecting\n\n- `mysqli_connect` opens the link\n"
FAKE_PDF = b"%PDF-1.7 fake"


@pytest.fixture(autouse=True)
def fake_pdf(monkeypatch):
    """A stand-in print, as in test_notes: Chrome takes seconds per page, and the real print is
    tested in test_pdf_render."""
    from studysystem.services import summaries

    monkeypatch.setattr(summaries, "render_pdf", lambda text, title: FAKE_PDF)


@pytest.fixture
def ch6(service_engine, user_id, web, tmp_path):  # noqa: F811
    """Chapter 6 fed to I3302 - no papers, no profile, no tagging: its `exam` is empty."""
    return material(service_engine, user_id, tmp_path / "Course Notes")


@pytest.fixture
def vault(tmp_path, monkeypatch):
    root = tmp_path / "vault"
    (root / FOLDER).mkdir(parents=True)
    monkeypatch.setenv("STUDYSYSTEM_VAULT_DIR", str(root))
    return root


@pytest.fixture
def declared(service_engine, user_id, web):  # noqa: F811
    set_course_input(service_engine, user_id, "I3302", "vault_folder", FOLDER)


def row_counts(engine) -> dict:
    with engine.connect() as conn:
        return {
            t.name: conn.execute(select(func.count()).select_from(t)).scalar_one()
            for t in metadata.sorted_tables
        }


def refused(call_it) -> StudyError:
    with pytest.raises(StudyError) as caught:
        call_it()
    return caught.value


# --- the task ------------------------------------------------------------------------------


def test_chapter_6_leaves_as_its_pages_and_the_steps(service_engine, user_id, ch6):
    payload = summary_task(service_engine, user_id, ch6).payload

    assert payload["material_id"] == ch6
    assert payload["material"] == "PHP_Chapter6_Eng_DataBase.pdf"
    assert [p["text"] for p in payload["content"]] == CH6_PAGES
    steps = " ".join(payload["rules"]["steps"])
    # the settled rules (build-plan 1.20, D-133), so an edit to the steps cannot pass silently
    rules = ("At most 900 words", "## Key terms", "Never 🎯", "from `exam` only", "## Exam focus")
    for rule in (*rules, "## To-Memorize section holding that table exactly"):
        assert rule in steps


def test_the_task_writes_nothing_and_can_be_asked_again(service_engine, user_id, ch6):
    before = row_counts(service_engine)

    first = summary_task(service_engine, user_id, ch6).payload
    second = summary_task(service_engine, user_id, ch6).payload

    assert first == second
    assert row_counts(service_engine) == before


def test_an_unknown_or_missing_material_is_refused(service_engine, user_id, ch6):
    assert refused(lambda: summary_task(service_engine, user_id, "01NOPE")).code == "not_found"
    assert refused(lambda: summary_task(service_engine, user_id, None)).code == "invalid_value"


def test_another_users_material_is_not_found(service_engine, other_user_id, ch6):
    assert refused(lambda: summary_task(service_engine, other_user_id, ch6)).code == "not_found"
    err = refused(lambda: save_summary(service_engine, other_user_id, ch6, SUMMARY))
    assert err.code == "not_found"


# --- the save ------------------------------------------------------------------------------


def test_the_summary_lands_as_md_and_pdf_with_its_stamp(
    service_engine, user_id, ch6, vault, declared
):
    before = row_counts(service_engine)

    out = save_summary(service_engine, user_id, ch6, SUMMARY)

    assert out == {
        "material": "PHP_Chapter6_Eng_DataBase.pdf",
        "md_path": f"{FOLDER}/summaries/{MD_FILE}",
        "pdf_path": f"{FOLDER}/summaries/{PDF_FILE}",
        "renamed": False,
    }
    text = (vault / out["md_path"]).read_text(encoding="utf-8")
    assert text.startswith("---\ntags: [study-system, summary]\ntype: summary\n")
    assert 'material: "PHP_Chapter6_Eng_DataBase.pdf"' in text
    assert "source-task" not in text  # a summary has no task
    assert text.endswith(SUMMARY)
    assert (vault / out["pdf_path"]).read_bytes() == FAKE_PDF
    assert row_counts(service_engine) == before  # no row, anywhere


def test_a_second_summary_takes_2_and_leaves_the_first(
    service_engine, user_id, ch6, vault, declared
):
    first = save_summary(service_engine, user_id, ch6, SUMMARY)
    second = save_summary(service_engine, user_id, ch6, "# Shorter\n")

    assert second["md_path"] == f"{FOLDER}/summaries/PHP_Chapter6_Eng_DataBase_summary (2).md"
    assert second["pdf_path"] == f"{FOLDER}/summaries/PHP_Chapter6_Eng_DataBase_summary (2).pdf"
    assert second["renamed"] is True
    assert (vault / first["md_path"]).read_text(encoding="utf-8").endswith(SUMMARY)


def test_a_summary_lands_apart_from_the_notes(service_engine, user_id, ch6, vault, declared):
    notes_dir = vault / FOLDER / "notes"
    notes_dir.mkdir()
    (notes_dir / "PHP_Chapter6_Eng_DataBase_notes.md").write_text("the note", encoding="utf-8")

    out = save_summary(service_engine, user_id, ch6, SUMMARY)

    assert out["md_path"] == f"{FOLDER}/summaries/{MD_FILE}"
    assert (notes_dir / "PHP_Chapter6_Eng_DataBase_notes.md").read_text() == "the note"
    assert sorted(f.name for f in notes_dir.iterdir()) == ["PHP_Chapter6_Eng_DataBase_notes.md"]


def test_an_empty_summary_is_refused(service_engine, user_id, ch6, vault, declared):
    err = refused(lambda: save_summary(service_engine, user_id, ch6, "  \n"))
    assert err.code == "invalid_value"
    assert not (vault / FOLDER / "summaries").exists()


def test_no_vault_root_saves_nothing(service_engine, user_id, ch6, declared):
    err = refused(lambda: save_summary(service_engine, user_id, ch6, SUMMARY))
    assert err.code == "vault_not_ready"
    assert "STUDYSYSTEM_VAULT_DIR" in err.fix
    assert "study_save_summary" in err.fix  # send the same text again


def test_no_vault_folder_saves_nothing(service_engine, user_id, ch6, vault):
    err = refused(lambda: save_summary(service_engine, user_id, ch6, SUMMARY))
    assert err.code == "vault_not_ready"
    assert "vault_folder" in err.fix
    assert not (vault / FOLDER / "summaries").exists()


def test_a_mistyped_folder_is_never_created(service_engine, user_id, ch6, vault):
    set_course_input(service_engine, user_id, "I3302", "vault_folder", "uni/Typo")

    err = refused(lambda: save_summary(service_engine, user_id, ch6, SUMMARY))

    assert err.code == "vault_not_ready"
    assert not (vault / "uni/Typo").exists()


def test_a_failed_pdf_keeps_the_md_and_says_so(
    service_engine, user_id, ch6, vault, declared, monkeypatch
):
    from studysystem.services import summaries

    def broken(text, title):
        raise OSError("no Chrome, Chromium or Edge found")

    monkeypatch.setattr(summaries, "render_pdf", broken)

    out = save_summary(service_engine, user_id, ch6, SUMMARY)

    assert out["pdf_path"] is None
    assert "no Chrome" in out["problem"]
    assert "(2)" in out["fix"]
    assert (vault / out["md_path"]).is_file()
    assert not (vault / FOLDER / "summaries" / PDF_FILE).exists()


# --- the tools -----------------------------------------------------------------------------


def test_both_tools_are_listed(service_engine):
    names = tool_names(create_server(service_engine))
    assert {"study_get_summary_task", "study_save_summary"} <= names


def test_a_summary_through_the_tools(call, ch6, vault, declared):
    task = call("study_get_summary_task", {"material_id": ch6})
    assert not task.is_error
    assert json.loads(task.content[0].text)["rules"]["steps"] == SUMMARY_STEPS

    saved = call("study_save_summary", {"material_id": ch6, "markdown": SUMMARY})

    assert not saved.is_error
    assert saved.structured_content["md_path"] == f"{FOLDER}/summaries/{MD_FILE}"


def test_a_refused_save_comes_back_as_its_error(call, ch6):
    saved = call("study_save_summary", {"material_id": ch6, "markdown": SUMMARY})

    assert saved.is_error
    assert saved.structured_content["code"] == "vault_not_ready"


# --- what the exams ask of the material (D-133) --------------------------------------------


def _topic_id(conn, name):
    return conn.execute(select(topic.c.id).where(topic.c.name == name)).scalar_one()


@pytest.fixture
def ch6_tagged(service_engine, user_id, i3302, tmp_path):  # noqa: F811
    """Chapter 6 fed after 1.13's three papers and profile v1, tagged to MySQL and regex."""
    material_id = material(service_engine, user_id, tmp_path / "Course Notes")
    with write_unit(service_engine) as conn:
        for name in (NAMES["mysql"], NAMES["regex"]):
            conn.execute(
                insert(coverage).values(
                    id=new_id(),
                    user_id=user_id,
                    topic_id=_topic_id(conn, name),
                    material_id=material_id,
                    tier="inferred",
                    created_at=now(),
                )
            )
    return {**i3302, "material_id": material_id}


def _expected(engine, profile_id, name, version=1):
    """One topic's entry as its rows say: its share in `profile_id`, its live past questions."""
    with engine.connect() as conn:
        topic_id = _topic_id(conn, name)
        share = conn.execute(
            select(topic_weight.c.weight).where(
                topic_weight.c.exam_profile_id == profile_id, topic_weight.c.topic_id == topic_id
            )
        ).scalar_one()
        asked = conn.execute(
            select(func.count())
            .select_from(practice_item)
            .where(
                practice_item.c.topic_id == topic_id,
                practice_item.c.origin == "past-exam",
                practice_item.c.state == "live",
            )
        ).scalar_one()
    exams = [{"exam": "Final exam", "share": round(share, 1), "profile_version": version}]
    return {"topic": name, "exams": exams, "past_questions": asked}


def _profile_v2(engine, user_id, v1_id, weights):
    """A second profile of v1's slot with {topic name: weight}. Returns its id."""
    with write_unit(engine) as conn:
        slot_id = conn.execute(
            select(exam_profile.c.slot_id).where(exam_profile.c.id == v1_id)
        ).scalar_one()
        v2 = new_id()
        conn.execute(
            insert(exam_profile).values(
                id=v2,
                owner_id=user_id,
                slot_id=slot_id,
                version=2,
                derived_at=now(),
                evidence_count_syllabus=0,
                evidence_count_instructor=0,
                evidence_count_format=0,
            )
        )
        for name, weight in weights.items():
            conn.execute(
                insert(topic_weight).values(
                    id=new_id(),
                    exam_profile_id=v2,
                    topic_id=_topic_id(conn, name),
                    weight=weight,
                    created_at=now(),
                )
            )
    return v2


def _note(engine, user_id, material_id, profile_id, body):
    with write_unit(engine) as conn:
        task_id = new_id()
        conn.execute(
            insert(generation_task).values(
                id=task_id,
                user_id=user_id,
                kind="note",
                scope_type="material",
                scope_id=material_id,
                route="host",
                status="closed",
                created_at=now(),
                closed_at=now(),
            )
        )
        conn.execute(
            insert(note).values(
                id=new_id(),
                user_id=user_id,
                material_id=material_id,
                task_id=task_id,
                section_ordinal=1,
                exam_profile_id=profile_id,
                body=body,
                created_at=now(),
            )
        )


def test_the_tagged_topics_come_with_their_shares_and_questions(
    service_engine, user_id, ch6_tagged
):
    exam = summary_task(service_engine, user_id, ch6_tagged["material_id"]).payload["exam"]

    expected = [
        _expected(service_engine, ch6_tagged["profile_id"], NAMES[key])
        for key in ("mysql", "regex")
    ]
    expected.sort(key=lambda t: (-t["exams"][0]["share"], -t["past_questions"], t["topic"]))
    assert exam["topics"] == expected  # most-weighted first
    assert all(t["past_questions"] > 0 for t in exam["topics"])  # the papers' own questions


def test_shares_come_from_the_latest_profile_only(service_engine, user_id, ch6_tagged):
    """v2 weights MySQL alone: regex has no share in it, even though v1 gave it one."""
    _profile_v2(service_engine, user_id, ch6_tagged["profile_id"], {NAMES["mysql"]: 50.0})

    exam = summary_task(service_engine, user_id, ch6_tagged["material_id"]).payload["exam"]

    by_name = {t["topic"]: t["exams"] for t in exam["topics"]}
    assert by_name[NAMES["mysql"]] == [{"exam": "Final exam", "share": 50.0, "profile_version": 2}]
    assert by_name[NAMES["regex"]] == []


def test_an_untagged_chapter_with_no_note_asks_for_a_plain_summary(service_engine, user_id, ch6):
    exam = summary_task(service_engine, user_id, ch6).payload["exam"]

    assert exam == {
        "topics": [],
        "note_targets": [],
        "note_profile": None,
        "note_memorize": None,
    }


NOTE_BODY = """# Chapter 6

🎯 likely exam target · 💡 tip · ⚠️ common mistake

## Prepared statements 🎯
- **🎯 Why it's an exam target:** 2020-02-17 Q2 asks the bind_param types
- a plain line, no target

## To-Memorize

| # | Statement / syntax / formula | Tier | Evidence |
|---|---|---|---|
| 1 | `bind_param("si", ...)`: one letter per value | E1 | 2020-02-17 Q2 |

---

## Self-check

| not | this table |
"""


def test_the_notes_target_lines_come_without_the_marker(service_engine, user_id, ch6_tagged):
    _note(service_engine, user_id, ch6_tagged["material_id"], ch6_tagged["profile_id"], NOTE_BODY)

    exam = summary_task(service_engine, user_id, ch6_tagged["material_id"]).payload["exam"]

    assert exam["note_targets"] == [  # the legend is skipped, the plain line left out
        "## Prepared statements",
        "- **Why it's an exam target:** 2020-02-17 Q2 asks the bind_param types",
    ]
    assert exam["note_profile"] == {"exam": "Final exam", "version": 1, "latest_version": 1}
    assert exam["note_memorize"] == (  # the table only, up to the next section
        "| # | Statement / syntax / formula | Tier | Evidence |\n|---|---|---|---|\n"
        '| 1 | `bind_param("si", ...)`: one letter per value | E1 | 2020-02-17 Q2 |'
    )


def test_a_note_from_an_older_profile_says_so(service_engine, user_id, ch6_tagged):
    _note(service_engine, user_id, ch6_tagged["material_id"], ch6_tagged["profile_id"], NOTE_BODY)
    _profile_v2(service_engine, user_id, ch6_tagged["profile_id"], {NAMES["mysql"]: 50.0})

    exam = summary_task(service_engine, user_id, ch6_tagged["material_id"]).payload["exam"]

    assert exam["note_profile"] == {"exam": "Final exam", "version": 1, "latest_version": 2}


def test_a_note_with_no_to_memorize_hands_no_table(service_engine, user_id, ch6_tagged):
    _note(service_engine, user_id, ch6_tagged["material_id"], None, "# Chapter 6\n\n- notes\n")

    exam = summary_task(service_engine, user_id, ch6_tagged["material_id"]).payload["exam"]

    assert exam["note_memorize"] is None
    assert exam["note_profile"] is None  # a note written with no profile


def test_a_topic_not_active_is_not_handed_over(service_engine, user_id, ch6_tagged):
    """D-133: only `active` topics - a proposed one tagged to the chapter is left out."""
    with write_unit(service_engine) as conn:
        course_id = conn.execute(
            select(topic.c.course_id).where(topic.c.name == NAMES["mysql"])
        ).scalar_one()
        proposed = new_id()
        conn.execute(
            insert(topic).values(
                id=proposed,
                user_id=user_id,
                course_id=course_id,
                name="PDO",
                status="proposed",
                proposed_by="profile",
                status_changed_at=now(),
                created_at=now(),
            )
        )
        conn.execute(
            insert(coverage).values(
                id=new_id(),
                user_id=user_id,
                topic_id=proposed,
                material_id=ch6_tagged["material_id"],
                tier="inferred",
                created_at=now(),
            )
        )

    exam = summary_task(service_engine, user_id, ch6_tagged["material_id"]).payload["exam"]

    assert {t["topic"] for t in exam["topics"]} == {NAMES["mysql"], NAMES["regex"]}


def test_only_live_past_exam_questions_are_counted(service_engine, user_id, ch6_tagged):
    """A past question parked as needs-review, and a generated item, are not past questions."""

    def mysql_asked():
        exam = summary_task(service_engine, user_id, ch6_tagged["material_id"]).payload["exam"]
        return next(t for t in exam["topics"] if t["topic"] == NAMES["mysql"])["past_questions"]

    before = mysql_asked()
    with write_unit(service_engine) as conn:
        mysql = _topic_id(conn, NAMES["mysql"])
        one, task_id = conn.execute(
            select(practice_item.c.id, practice_item.c.task_id).where(
                practice_item.c.topic_id == mysql
            )
        ).first()
        conn.execute(
            update(practice_item).where(practice_item.c.id == one).values(state="needs-review")
        )
        conn.execute(
            insert(practice_item).values(
                id=new_id(),
                user_id=user_id,
                topic_id=mysql,
                task_id=task_id,
                item_ordinal=999,
                origin="generated",
                source_marker="model-knowledge",
                question="a generated MySQL question",
                answer_provenance="none",
                novelty_score=0.5,
                created_at=now(),
            )
        )

    assert mysql_asked() == before - 1


def test_a_topic_on_two_exams_ranks_by_its_largest_share(service_engine, user_id, ch6_tagged):
    """Regex is small on the Final but 90% of a Partial: it comes first, its Partial first."""
    with write_unit(service_engine) as conn:
        course_id = conn.execute(
            select(topic.c.course_id).where(topic.c.name == NAMES["regex"])
        ).scalar_one()
        partial = new_id()
        conn.execute(
            insert(assessment_slot).values(
                id=partial,
                owner_id=user_id,
                course_id=course_id,
                name="Partial exam",
                kind="exam",
                created_at=now(),
            )
        )
        profile_id = new_id()
        conn.execute(
            insert(exam_profile).values(
                id=profile_id,
                owner_id=user_id,
                slot_id=partial,
                version=1,
                derived_at=now(),
                evidence_count_syllabus=0,
                evidence_count_instructor=0,
                evidence_count_format=0,
            )
        )
        conn.execute(
            insert(topic_weight).values(
                id=new_id(),
                exam_profile_id=profile_id,
                topic_id=_topic_id(conn, NAMES["regex"]),
                weight=90.0,
                created_at=now(),
            )
        )

    exam = summary_task(service_engine, user_id, ch6_tagged["material_id"]).payload["exam"]

    first = exam["topics"][0]
    assert first["topic"] == NAMES["regex"]
    assert [e["exam"] for e in first["exams"]] == ["Partial exam", "Final exam"]


def test_the_weighting_steps_are_the_settled_ones():
    """D-133's two rules the first steps test does not pin."""
    steps = " ".join(SUMMARY_STEPS)

    assert "put each one's numbers beside its heading" in steps
    assert "older profile: where they disagree with exam.topics, exam.topics wins" in steps


# --- in parts (D-145) ----------------------------------------------------------------------


def test_a_summary_too_big_for_one_reply_comes_in_parts(service_engine, user_id, ch6, monkeypatch):
    """Paged by its pages alone - the exam evidence rides in part 1. One page a part here; where
    the cut falls is `split`'s job (test_parts). No row, so any order is served."""
    from studysystem.services import generation
    from studysystem.services.parts import Part

    monkeypatch.setattr(
        generation, "split", lambda head, pages, extras: [Part(pages=[i]) for i in range(3)]
    )

    third = summary_task(service_engine, user_id, ch6, part=3).payload
    first = summary_task(service_engine, user_id, ch6, part=1).payload

    assert set(first) == {"material_id", "material", "part", "parts", "content", "exam", "rules"}
    assert set(third) == {"material_id", "part", "parts", "content"}
    assert (third["part"], third["parts"], [p["page"] for p in third["content"]]) == (3, 3, [3])
    with pytest.raises(StudyError) as excinfo:
        summary_task(service_engine, user_id, ch6, part=4)
    assert excinfo.value.field_errors[0]["field"] == "part"
