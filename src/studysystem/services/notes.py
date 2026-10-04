"""The note export (D-10, D-72, D-73, D-78, D-79, D-81): the database holds a note's body; the vault
gets two one-way copies beside each other - the stamped markdown, and a PDF printed from the
same text, the copy he studies from - never the source of truth.

The export runs after the submission's write unit commits, so a failed file write never rolls
back a note: the note stays in the database with `exported_path` or `pdf_path` NULL, and the
next export writes whichever file is missing.

A file is never overwritten - a name already taken gets the first free `(2)`, `(3)`... so a new
version of a chapter never lands on the old note and its annotations. The md and the PDF take
that name as a pair: one file alone takes it for both.

The export never raises. The note is already committed when it runs, so an error here would tell
the host its note failed when it did not; every problem comes back as an entry with its fix.

The vault root is machine config, like the data dir: `STUDYSYSTEM_VAULT_DIR`. Each course names
its own folder under it (`course.vault_folder`), and both paths are stored relative to the
root, with `/`, so they survive the vault moving to another drive or machine.

Accepted risk: a file written whose path then fails to save is in the vault while the database
says it is not - the next export writes the md again as `(2)`, or finds the PDF in its way.
"""

import json
import os
from pathlib import Path

from sqlalchemy import ColumnElement, Engine, or_, select, update
from sqlalchemy.exc import SQLAlchemyError

from studysystem.db.tables import course, exam_profile, material, note
from studysystem.services.lookups import find_course
from studysystem.services.pdf_render import render_pdf
from studysystem.services.units import write_unit

VAULT_ENV = "STUDYSYSTEM_VAULT_DIR"
# how every fix that leaves a file unwritten ends: the note waits safely, the tool sends it (D-79)
RETRY = "then run study_export_notes for this course - the note is saved in the database meanwhile"


def vault_dir() -> Path | None:
    """The vault root the exports go under, or None when this machine has not set one."""
    out = os.environ.get(VAULT_ENV, "").strip()
    return Path(out) if out else None


def export_notes(engine: Engine, user_id: str, task_id: str) -> list[dict]:
    """Write every file this task's notes are missing - the md, the PDF or both - into the
    course's `notes/` folder, and record where each went. The submit's export (D-72). Returns
    one entry per note it tried, in order: {"section", "material", "exported_path",
    "pdf_path", "renamed"}, plus "problem" and "fix" when a path is still None. A task whose
    notes have both files returns []."""
    return _export_where(engine, user_id, note.c.task_id == task_id)


def export_course_notes(
    engine: Engine, user_id: str, code: str, semester_name: str | None = None
) -> dict:
    """`study_export_notes` (D-79): the same export over every note of a course - Chapter 6's
    missing PDF, a note saved before the vault folder was declared, a write that failed.
    Idempotent: a second call finds nothing missing and writes nothing. Returns {"course",
    "exports"}, the entries as export_notes gives them - "course" as stored (`I3302`), however
    the code was typed. An unknown code is `not_found`."""
    with engine.connect() as conn:
        course_id = find_course(conn, user_id, code, semester_name)
        stored = conn.execute(select(course.c.code).where(course.c.id == course_id)).scalar_one()
    return {"course": stored, "exports": _export_where(engine, user_id, course.c.id == course_id)}


def _export_where(engine: Engine, user_id: str, scope: ColumnElement[bool]) -> list[dict]:
    """Export the notes in `scope` (one task, or one course) that miss a file. Never raises:
    even the first read failing comes back as an entry, the notes being saved already."""
    try:
        rows = _unexported_notes(engine, user_id, scope)
    except SQLAlchemyError:
        return [
            _unexported(
                None,
                None,
                "the notes could not be read for export",
                "nothing was written to the vault; " + RETRY,
            )
        ]
    if not rows:
        return []
    return _export(engine, rows)


def _unexported_notes(engine: Engine, user_id: str, scope: ColumnElement[bool]) -> list:
    """The notes in `scope` missing their md or their PDF, oldest first, with what their files
    need: where the md went (NULL = not written), the task and material that made them, the
    course's vault folder and the profile's version."""
    with engine.connect() as conn:
        return list(
            conn.execute(
                select(
                    note.c.id,
                    note.c.task_id,
                    note.c.section_ordinal,
                    note.c.body,
                    note.c.exported_path,
                    material.c.filename,
                    course.c.vault_folder,
                    exam_profile.c.version,
                )
                .join(material, material.c.id == note.c.material_id)
                .join(course, course.c.id == material.c.course_id)
                .outerjoin(exam_profile, exam_profile.c.id == note.c.exam_profile_id)
                .where(
                    scope,
                    note.c.user_id == user_id,
                    or_(note.c.exported_path.is_(None), note.c.pdf_path.is_(None)),
                )
                .order_by(note.c.created_at, note.c.section_ordinal)
            ).all()
        )


def _export(engine: Engine, rows: list) -> list[dict]:
    """Write each note's files and record their paths - the guards first, once (D-72)."""
    # One course, one vault folder - a task's notes or a course's: the guards run once (D-72).
    root = vault_dir()
    folder = rows[0].vault_folder
    if root is None:
        problem = (
            "this machine has no vault root set",
            f"set {VAULT_ENV} to the vault's study folder in the server's config, restart "
            "it, " + RETRY,
        )
    elif folder is None:
        problem = (
            "the course has no vault folder declared",
            "declare it with study_set_course_input(field='vault_folder'), e.g. "
            "'uni/Semester 1 2026-2027/Server-Side Web Development', " + RETRY,
        )
    elif not (root / folder).is_dir():
        # never created: a typo in vault_folder must not grow a new course folder in the vault
        problem = (
            f"{folder} is not a folder under the vault root",
            "fix vault_folder with study_set_course_input, " + RETRY,
        )
    else:
        problem = None
    if problem is not None:
        return [_unexported(r.section_ordinal, r.filename, *problem) for r in rows]
    assert root is not None and folder is not None  # the guards above return otherwise

    out = []
    for r in rows:
        # the md and the PDF carry the same text: the database's body, never the md on disk,
        # which may hold his annotations (D-10)
        text = _stamp(r.version, r.task_id, r.filename, r.section_ordinal) + r.body
        md_relative = r.exported_path
        renamed = False
        if md_relative is None:
            # no md yet (a new note): pick a pair free as both md and PDF, write the md and
            # record it at once, so a crash before the PDF never writes the md twice (D-79)
            try:
                notes_dir = root / folder / "notes"
                notes_dir.mkdir(exist_ok=True)  # notes/ only - its course folder exists
                name = _note_name(r.filename, r.section_ordinal)
                target_md, target_pdf = _free_name(notes_dir, name)
                # "x" creates or fails, so a file that appeared since _free_name looked is never
                # overwritten; utf-8 because a note carries 🎯 and Windows' default codec cannot
                with target_md.open("x", encoding="utf-8", newline="\n") as f:
                    f.write(text)
                md_relative = f"{folder}/notes/{target_md.name}"
                _record(engine, r.id, exported_path=md_relative)
            except OSError as e:
                # no md, no PDF: a PDF alone would take the pair's name (D-78)
                out.append(
                    _unexported(
                        r.section_ordinal,
                        r.filename,
                        f"the file could not be written: {e.strerror or e}",
                        "check the vault folder is writable, " + RETRY,
                    )
                )
                continue
            except SQLAlchemyError:
                out.append(
                    _unexported(
                        r.section_ordinal,
                        r.filename,
                        f"{md_relative} was written, but where it went could not be saved",
                        "the note is safe in the database; the file is in the vault",
                    )
                )
                continue
            renamed = target_md.name != f"{name}.md"
            pdf_relative = f"{folder}/notes/{target_pdf.name}"
        else:
            # the md is out already (Chapter 6): its PDF takes the md's recorded name, so a
            # `(2)` md gets a `(2)` PDF (D-78)
            pdf_relative = md_relative.removesuffix(".md") + ".pdf"

        entry = {
            "section": r.section_ordinal,
            "material": r.filename,
            "exported_path": md_relative,
            "pdf_path": None,
            "renamed": renamed,
        }
        problem = _write_pdf(engine, r.id, root / pdf_relative, pdf_relative, text)
        if problem is None:
            entry["pdf_path"] = pdf_relative
        else:
            entry["problem"], entry["fix"] = problem
        out.append(entry)
    return out


def _write_pdf(
    engine: Engine, note_id: str, target: Path, relative: str, text: str
) -> tuple[str, str] | None:
    """Render the note's PDF, write it create-only and record `pdf_path` (D-78, D-81).
    Returns None when all three happened, or the (problem, fix) that stopped it - the md is
    already in the vault either way, so the fix points at study_export_notes (D-79)."""
    try:
        data = render_pdf(text, target.stem)
    except (RuntimeError, OSError) as e:
        return (
            f"the PDF could not be made: {e}",
            "the md is in the vault; study_export_notes tries the PDF again",
        )
    try:
        # "xb": create-only, in bytes - a PDF is binary, not text
        with target.open("xb") as f:
            f.write(data)
    except FileExistsError:
        return (
            f"{target.name} is already in the vault, and an export never overwrites it (D-73)",
            "move or rename that file, then run study_export_notes",
        )
    except OSError as e:
        return (
            f"the PDF could not be written: {e.strerror or e}",
            "check the vault folder is writable, then run study_export_notes",
        )
    try:
        _record(engine, note_id, pdf_path=relative)
    except SQLAlchemyError:
        return (
            f"{relative} was written, but where it went could not be saved",
            "the PDF is in the vault; the database does not know it yet",
        )
    return None


def _record(engine: Engine, note_id: str, **paths: str) -> None:
    """Save where a note's file went - one write unit per save (D-79)."""
    with write_unit(engine) as conn:
        conn.execute(update(note).where(note.c.id == note_id).values(**paths))


def _unexported(section: int | None, material: str | None, problem: str, fix: str) -> dict:
    """One note the export could not write, and how to fix that - `section` and `material`
    None when the notes could not even be read."""
    return {
        "section": section,
        "material": material,
        "exported_path": None,
        "pdf_path": None,
        "problem": problem,
        "fix": fix,
    }


def _note_name(filename: str, section: int) -> str:
    """The file name a note asks for, before any clash: the material's name plus `_notes`, and
    ` - part n` from section 2 on - never `(n)`, which marks a clash (D-73)."""
    stem = f"{Path(filename).stem}_notes"
    return stem if section == 1 else f"{stem} - part {section}"


def _free_name(folder: Path, stem: str) -> tuple[Path, Path]:
    """The first md + PDF pair in `folder` with neither file taken yet: `stem.md` +
    `stem.pdf`, then `stem (2)`, `stem (3)`... (D-73). One file alone takes the name for both,
    so a note's md and PDF always share a stem (D-78)."""
    target_md = folder / f"{stem}.md"
    target_pdf = folder / f"{stem}.pdf"
    n = 2
    while target_md.exists() or target_pdf.exists():
        target_md = folder / f"{stem} ({n}).md"
        target_pdf = folder / f"{stem} ({n}).pdf"
        n += 1
    return target_md, target_pdf


def _stamp(profile_version: int | None, task_id: str, material: str, section: int) -> str:
    """The frontmatter that marks a file as a generated export (D-10, D-72), in the vault's own
    key style - `tags`, `type` and hyphenated keys, so `profile-version` is the key the vault's
    staleness check already reads (SYSTEM.md Pipeline B). Strings are written as JSON strings,
    which YAML reads as double-quoted text - so a filename with a `:` or a `#` stays one value.
    A course with no profile stamps `profile-version: null` (D-71)."""
    lines = [
        "---",
        "tags: [study-system, note]",
        "type: note",
        "generated: true",
        f"profile-version: {'null' if profile_version is None else profile_version}",
        f"source-task: {json.dumps(task_id)}",
        f"material: {json.dumps(material)}",
        f"section: {section}",
        "---",
        "",
    ]
    return "\n".join(lines)
