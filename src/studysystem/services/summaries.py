"""Chapter summaries (1.20, D-131): a short reread of one material, apart from its note. Two
steps, and no row anywhere - nothing else in the system reads a summary:
- `summary_task` hands the host the material's pages, what the exams ask of it (D-133) and how
  to write the summary - the same pages a note task carries, read-only, so it can be asked for
  again and again;
- `save_summary` writes the host's text into the course's `summaries/` folder (D-215) as
  `<material stem>_summary.md` with its PDF beside it, the notes' first free name, never
  overwriting (D-73, D-78).

Unlike the note export, a save raises when the vault is not ready: the summary lives only in the
host's reply, so the host must hear "not saved" and keep the text to send again.

Accepted (D-131): no record of the files, so a moved or deleted summary goes unnoticed; a chapter
with only a summary is not taught (D-120 reads notes and sessions).
"""

import json
import logging
from pathlib import Path

from sqlalchemy import Connection, Engine, func, select

from studysystem.db.tables import (
    assessment_slot,
    course,
    coverage,
    exam_profile,
    material,
    note,
    practice_item,
    topic,
    topic_weight,
)
from studysystem.errors import StudyError
from studysystem.services import notes, values
from studysystem.services.generation import (
    Task,
    _find_material,
    _pages,
    in_range,
    layout,
    part_number,
)
from studysystem.services.pdf_render import render_pdf
from studysystem.services.units import read_unit

log = logging.getLogger(__name__)

TARGET = "🎯"
LEGEND = "likely exam target"  # the note's legend line carries the marker and is no target
MEMORIZE_HEADING = "## To-Memorize"  # the note's section the summary ends with (D-133)

# How to write the summary. It is weighed by the exam only through what the task hands over
# (D-133): the profile's shares and the note's exam-target lines. Never 🎯 - its legend means
# "likely exam target" and is the note's to justify.
SUMMARY_STEPS = [
    "Write one summary of this material: all of it, compressed for a fast reread.",
    "Weigh it by the exam, from `exam` only: give more room to the topics in exam.topics with "
    "the largest shares and most past questions, and put each one's numbers beside its heading, "
    "e.g. '(14.6% of the Final exam, 25 past questions)'. Make no other exam claim.",
    "Right after the # title, a ## Exam focus section: the two or three topics of exam.topics "
    "with the largest shares, each with its numbers, then each of exam.note_targets as a plain "
    "bullet naming its paper as the line does. Leave the section out when both lists are empty.",
    "When exam.note_profile.version is below its latest_version, the note's lines come from an "
    "older profile: where they disagree with exam.topics, exam.topics wins.",
    "Read every page before writing: the last pages often show what the material builds toward.",
    "Write in the language of the pages, and keep their terminology, formulas and syntax exactly.",
    "At most 900 words; shorter when the material is short. The To-Memorize table below does not "
    "count.",
    "Layout: a # title, Exam focus, then one ## heading per main part of the material, in its "
    "order, with short bullets under each - no paragraphs of prose.",
    "Give a worked example only where an idea cannot be read without one, and keep it to a few "
    "lines.",
    "Then a ## Key terms table: | Term | Meaning |.",
    "End, when exam.note_memorize is not null, with a ## To-Memorize section holding that table "
    "exactly as given - every row, its Tier and Evidence columns too: it is the note's, already "
    "checked against the papers.",
    "Markers, at the start of a line only: 💡 tip or shortcut; ⚠️ common mistake. Never 🎯 - a "
    "summary marks no exam target; its numbers say what the exam asks. No raw HTML. "
    "No frontmatter: the server adds it.",
    "Save it with study_save_summary(material_id, markdown): the whole summary as one markdown "
    "text.",
]

# the fix's ending when a save wrote nothing: the text is only in the host's reply
RESEND = "then call study_save_summary again with the same text - nothing was saved"


def summary_task(engine: Engine, user_id: str, material_id: str | None, part: object = 1) -> Task:
    """Part `part` of the material's pages, what the exams ask of it and the summary rules:
    {"material_id", "part", "parts", "content", "exam", "rules"}, then that part's page pictures;
    a later part carries only "material_id", "part", "parts" and its pages (D-145). Writes
    nothing - asking twice hands back the same thing."""
    if material_id is None:
        raise values.invalid(
            "material_id", "is missing", "send the material_id that study_add_material returned"
        )
    with read_unit(engine) as conn:
        found = _find_material(conn, user_id, material_id)
        exam = _exam_weight(conn, user_id, found.id)
    content, pictures = _pages(found.file_ref)
    payload = {
        "material_id": found.id,
        "material": found.filename,
        "content": content,
        "exam": exam,
        "rules": {"steps": SUMMARY_STEPS},
    }
    # Paged like a task (D-145), pages only: the exam evidence is small and rides in part 1.
    # No row, so nothing checks the order - the tool text asks for every part.
    n = part_number(part)
    cut = layout(Task(payload=payload, pictures=pictures), None, ("material_id",))
    in_range(n, cut)
    sent = cut.build(n)
    log.info(
        json.dumps(
            {
                "event": "summary_task",
                "material_id": found.id,
                "pages": len(content),
                "pictures": len(pictures),
                "topics": len(exam["topics"]),
                "note_targets": len(exam["note_targets"]),
                "to_memorize": exam["note_memorize"] is not None,
                "part": n,
                "parts": len(cut.parts),
            }
        )
    )
    return sent


def _exam_weight(conn: Connection, user_id: str, material_id: str) -> dict:
    """What the exams ask of one material (D-133), two sources:
    - "topics": each `active` topic its tagging links to it (1.15b), most-weighted first, with its
      share in each exam's **latest** profile and its live past-exam questions;
    - "note_targets": the lines its note marked 🎯 (marker dropped, legend skipped), and
      "note_profile": the exam and profile version the note was written from, beside that
      slot's latest version - None with no note;
    - "note_memorize": the note's To-Memorize table, as written - None with no note.

    I3304 chapter 1 on 2026-10-10 -> frame hex decoding (Final 14.6, Partial 7.8; 25 questions),
    IP addressing (Final 13.2; 17), protocol layers (Final 1.1; 2); 3 note lines - MAC/IP per hop
    (NAT 20.0% + frame hex 7.8%) among them; note from the Partial's profile v1, latest v1; its
    9-row To-Memorize table.
    """
    rows = conn.execute(
        select(topic.c.id, topic.c.name)
        .join_from(coverage, topic, topic.c.id == coverage.c.topic_id)
        .where(
            coverage.c.user_id == user_id,
            coverage.c.material_id == material_id,
            topic.c.status == "active",
        )
    ).all()
    ids = [row.id for row in rows]

    # A second copy of exam_profile, so "latest" reads its own rows - as in plan._study_rows.
    newer = exam_profile.alias("newer")
    latest = (
        select(func.max(newer.c.version))
        .where(newer.c.slot_id == exam_profile.c.slot_id)
        .scalar_subquery()
    )
    shares = {}
    for share in conn.execute(
        select(
            topic_weight.c.topic_id,
            assessment_slot.c.name,
            exam_profile.c.version,
            topic_weight.c.weight,
        )
        .join_from(topic_weight, exam_profile, exam_profile.c.id == topic_weight.c.exam_profile_id)
        .join(assessment_slot, assessment_slot.c.id == exam_profile.c.slot_id)
        .where(
            exam_profile.c.owner_id == user_id,
            exam_profile.c.version == latest,
            topic_weight.c.topic_id.in_(ids),
        )
        .order_by(topic_weight.c.weight.desc(), assessment_slot.c.name)
    ):
        shares.setdefault(share.topic_id, []).append(
            {"exam": share.name, "share": round(share.weight, 1), "profile_version": share.version}
        )
    asked = dict(
        conn.execute(
            select(practice_item.c.topic_id, func.count(practice_item.c.id))
            .where(
                practice_item.c.user_id == user_id,
                practice_item.c.topic_id.in_(ids),
                practice_item.c.origin == "past-exam",
                practice_item.c.state == "live",
            )
            .group_by(practice_item.c.topic_id)
        )
        .tuples()
        .all()
    )
    topics = [
        {"topic": row.name, "exams": shares.get(row.id, []), "past_questions": asked.get(row.id, 0)}
        for row in rows
    ]
    # most-weighted first: its largest share, then its past questions, then its name
    topics.sort(
        key=lambda t: (
            -max((e["share"] for e in t["exams"]), default=0.0),
            -t["past_questions"],
            t["topic"],
        )
    )

    sections = conn.execute(
        select(note.c.body, note.c.exam_profile_id)
        .where(note.c.user_id == user_id, note.c.material_id == material_id)
        .order_by(note.c.created_at, note.c.section_ordinal)
    ).all()
    targets = [
        line.replace(f"{TARGET} ", "").replace(TARGET, "").strip()
        for section in sections
        for line in section.body.splitlines()
        if TARGET in line and LEGEND not in line
    ]
    memorize = next(
        (table for s in sections if (table := _memorize_table(s.body)) is not None), None
    )
    note_profile = None
    profile_id = next((s.exam_profile_id for s in sections if s.exam_profile_id), None)
    if profile_id is not None:
        found = conn.execute(
            select(assessment_slot.c.name, exam_profile.c.version, latest.label("latest"))
            .join_from(
                exam_profile, assessment_slot, assessment_slot.c.id == exam_profile.c.slot_id
            )
            .where(exam_profile.c.id == profile_id)
        ).one()
        note_profile = {
            "exam": found.name,
            "version": found.version,
            "latest_version": found.latest,
        }
    return {
        "topics": topics,
        "note_targets": targets,
        "note_profile": note_profile,
        "note_memorize": memorize,
    }


def _memorize_table(body: str) -> str | None:
    """The table under the note's `## To-Memorize` heading, as written - its rows carry their
    tier and evidence. None when the note has no such section, or the section holds no table.

    I3304 chapter 1 -> 9 rows, from "Frame = [D.MAC][S.MAC][Type] ..." (E1) to Manchester (E3).
    """
    lines = body.splitlines()
    start = next((n for n, line in enumerate(lines) if line.strip() == MEMORIZE_HEADING), None)
    if start is None:
        return None
    table = []
    for line in lines[start + 1 :]:
        if line.startswith("#") or line.strip() == "---":
            break  # the next section
        if line.lstrip().startswith("|"):
            table.append(line.strip())
    return "\n".join(table) or None


def save_summary(engine: Engine, user_id: str, material_id: str, markdown: str) -> dict:
    """Write the summary's md, then its PDF, under the course's `summaries/`. Returns {"material",
    "md_path", "pdf_path", "renamed"} - paths relative to the vault root, with `/` - plus
    "problem" and "fix" when the md went out but its PDF did not. Raises when nothing could be
    written, so the host keeps its text."""
    if not markdown.strip():
        raise values.invalid("markdown", "is empty", "send the whole summary as markdown")

    with read_unit(engine) as conn:
        found = conn.execute(
            select(material.c.id, material.c.filename, course.c.vault_folder)
            .join(course, course.c.id == material.c.course_id)
            .where(material.c.id == material_id, material.c.user_id == user_id)
        ).one_or_none()
    if found is None:
        raise StudyError(
            code="not_found",
            message=f"no material {material_id}",
            fix="use a material_id that study_add_material returned",
            field_errors=[{"field": "material_id", "problem": "no such material"}],
        )

    # the note export's guards, raised instead of reported (module docstring)
    root = notes.vault_dir()
    folder = found.vault_folder
    if root is None:
        raise _not_ready(
            "this machine has no vault root set",
            f"set {notes.VAULT_ENV} to the vault's study folder in the server's config, "
            "restart it, " + RESEND,
        )
    if folder is None:
        raise _not_ready(
            "the course has no vault folder declared",
            "declare it with study_set_course_input(field='vault_folder'), " + RESEND,
        )
    if not (root / folder).is_dir():
        # never created: a typo in vault_folder must not grow a new course folder in the vault
        raise _not_ready(
            f"{folder} is not a folder under the vault root",
            "fix vault_folder with study_set_course_input, " + RESEND,
        )

    text = _stamp(found.filename) + markdown.rstrip("\n") + "\n"
    name = f"{Path(found.filename).stem}_summary"
    try:
        summaries_dir = root / folder / "summaries"
        summaries_dir.mkdir(exist_ok=True)  # summaries/ only - its course folder exists
        target_md, target_pdf = notes._free_name(summaries_dir, name)
        # "x" creates or fails: a file that appeared since _free_name looked is never overwritten
        with target_md.open("x", encoding="utf-8", newline="\n") as f:
            f.write(text)
    except OSError as e:
        raise StudyError(
            code="write_failed",
            message=f"the summary could not be written: {e.strerror or e}",
            fix="check the vault folder is writable, " + RESEND,
        ) from e

    out = {
        "material": found.filename,
        "md_path": f"{folder}/summaries/{target_md.name}",
        "pdf_path": None,
        "renamed": target_md.name != f"{name}.md",
    }
    problem = _write_pdf(target_pdf, text)
    if problem is None:
        out["pdf_path"] = f"{folder}/summaries/{target_pdf.name}"
    else:
        out["problem"], out["fix"] = problem

    log.info(json.dumps({"event": "save_summary", "material_id": found.id, **out}))
    return out


def _write_pdf(target: Path, text: str) -> tuple[str, str] | None:
    """Print the summary's PDF and write it create-only. None when it is written, or the
    (problem, fix) that stopped it - the md is in the vault either way."""
    later = (
        "the md is in the vault; once fixed, save again for a PDF - the new pair lands as (2), "
        "so delete the lone md"
    )
    try:
        data = render_pdf(text, target.stem)
    except (RuntimeError, OSError) as e:
        return f"the PDF could not be made: {e}", later
    try:
        # "xb": create-only, in bytes - a PDF is binary, not text
        with target.open("xb") as f:
            f.write(data)
    except OSError as e:
        return f"the PDF could not be written: {e.strerror or e}", later
    return None


def _not_ready(problem: str, fix: str) -> StudyError:
    return StudyError(code="vault_not_ready", message=problem, fix=fix)


def _stamp(material_name: str) -> str:
    """The frontmatter of a summary file, in the notes' key style (notes._stamp). No
    `source-task` or `profile-version`: a summary has no task, and the shares it is weighed by
    (D-133) come from several exams' latest profiles, not one."""
    lines = [
        "---",
        "tags: [study-system, summary]",
        "type: summary",
        "generated: true",
        f"material: {json.dumps(material_name)}",
        "---",
        "",
    ]
    return "\n".join(lines)
