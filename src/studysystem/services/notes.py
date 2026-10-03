"""The note export (D-10, D-72, D-73): the database holds a note's body; the vault gets a one-way
copy, stamped so it reads as generated, never as the source of truth.

The export runs after the submission's write unit commits, so a failed file write never rolls
back a note: the note stays in the database with `exported_path` NULL. Nothing exports it later
yet - re-export is carried to task 1.14b (D-76).

A file is never overwritten - a name already taken gets the first free `(2)`, `(3)`... so a new
version of a chapter never lands on the old note and its annotations.

The export never raises. The note is already committed when it runs, so an error here would tell
the host its note failed when it did not; every problem comes back as an entry with its fix.

The vault root is machine config, like the data dir: `STUDYSYSTEM_VAULT_DIR`. Each course names
its own folder under it (`course.vault_folder`), and `exported_path` is stored relative to the
root, with `/`, so it survives the vault moving to another drive or machine.

Accepted risk: a file written whose `exported_path` then fails to save is in the vault while the
database says it is not - the re-export 1.14b brings would write it again as `(2)`.
"""

import json
import os
from pathlib import Path

from sqlalchemy import Engine, select, update
from sqlalchemy.exc import SQLAlchemyError

from studysystem.db.tables import course, exam_profile, material, note
from studysystem.services.units import write_unit

VAULT_ENV = "STUDYSYSTEM_VAULT_DIR"


def vault_dir() -> Path | None:
    """The vault root the exports go under, or None when this machine has not set one."""
    out = os.environ.get(VAULT_ENV, "").strip()
    return Path(out) if out else None


def export_notes(engine: Engine, user_id: str, task_id: str) -> list[dict]:
    """Write every note of this task that is not exported yet into its course's `notes/` folder,
    and record where each went. Returns one entry per note it tried, in section order:
    {"section", "exported_path", "renamed"} when written, {"section", "exported_path": None,
    "problem", "fix"} when not. A task whose notes are all exported returns []."""
    try:
        rows = _unexported_notes(engine, user_id, task_id)
    except SQLAlchemyError:
        return [
            _unexported(
                None,
                "the notes could not be read for export",
                "the note is safe in the database; it was not written to the vault",
            )
        ]
    if not rows:
        return []
    return _export(engine, task_id, rows)


def _unexported_notes(engine: Engine, user_id: str, task_id: str) -> list:
    """The task's notes not exported yet, with what their file needs: the material's name,
    the course's vault folder and the profile's version."""
    with engine.connect() as conn:
        return list(
            conn.execute(
                select(
                    note.c.id,
                    note.c.section_ordinal,
                    note.c.body,
                    material.c.filename,
                    course.c.vault_folder,
                    exam_profile.c.version,
                )
                .join(material, material.c.id == note.c.material_id)
                .join(course, course.c.id == material.c.course_id)
                .outerjoin(exam_profile, exam_profile.c.id == note.c.exam_profile_id)
                .where(
                    note.c.task_id == task_id,
                    note.c.user_id == user_id,
                    note.c.exported_path.is_(None),
                )
                .order_by(note.c.section_ordinal)
            ).all()
        )


def _export(engine: Engine, task_id: str, rows: list) -> list[dict]:
    """Write each note's file and record its path - the guards first, once (D-72)."""
    # One task, one material, one course: the guards run once (D-72).
    root = vault_dir()
    folder = rows[0].vault_folder
    if root is None:
        problem = (
            "this machine has no vault root set",
            f"set {VAULT_ENV} to the vault's study folder in the server's config and restart "
            "it - notes after that export; this one stays saved in the database",
        )
    elif folder is None:
        problem = (
            "the course has no vault folder declared",
            "declare it with study_set_course_input(field='vault_folder'), e.g. "
            "'uni/Semester 1 2026-2027/Server-Side Web Development' - notes after that "
            "export; this one stays saved in the database",
        )
    elif not (root / folder).is_dir():
        # never created: a typo in vault_folder must not grow a new course folder in the vault
        problem = (
            f"{folder} is not a folder under the vault root",
            "fix vault_folder with study_set_course_input - notes after that export; this one "
            "stays saved in the database",
        )
    else:
        problem = None
    if problem is not None:
        return [_unexported(r.section_ordinal, *problem) for r in rows]
    assert root is not None and folder is not None  # the guards above return otherwise

    out = []
    for r in rows:
        relative = None
        try:
            notes_dir = root / folder / "notes"
            notes_dir.mkdir(exist_ok=True)  # notes/ only - its course folder exists
            name = _note_name(r.filename, r.section_ordinal)
            target = _free_name(notes_dir, name)
            # "x" creates or fails, so a file that appeared since _free_name looked is never
            # overwritten; utf-8 because a note carries 🎯 and Windows' default codec cannot
            with target.open("x", encoding="utf-8", newline="\n") as f:
                f.write(_stamp(r.version, task_id, r.filename, r.section_ordinal) + r.body)
            relative = f"{folder}/notes/{target.name}"
            with write_unit(engine) as conn:
                conn.execute(update(note).where(note.c.id == r.id).values(exported_path=relative))
        except OSError as e:
            out.append(
                _unexported(
                    r.section_ordinal,
                    f"the file could not be written: {e.strerror or e}",
                    "check the vault folder is writable; the note is safe in the database",
                )
            )
            continue
        except SQLAlchemyError:
            out.append(
                _unexported(
                    r.section_ordinal,
                    f"{relative} was written, but where it went could not be saved",
                    "the note is safe in the database; the file is in the vault",
                )
            )
            continue
        out.append(
            {
                "section": r.section_ordinal,
                "exported_path": relative,
                "renamed": target.name != f"{name}.md",
            }
        )
    return out


def _unexported(section: int | None, problem: str, fix: str) -> dict:
    """One note the export could not write, and how to fix that - `section` None when the
    notes could not even be read."""
    return {"section": section, "exported_path": None, "problem": problem, "fix": fix}


def _note_name(filename: str, section: int) -> str:
    """The file name a note asks for, before any clash: the material's name plus `_notes`, and
    ` - part n` from section 2 on - never `(n)`, which marks a clash (D-73)."""
    stem = f"{Path(filename).stem}_notes"
    return stem if section == 1 else f"{stem} - part {section}"


def _free_name(folder: Path, stem: str) -> Path:
    """The first `.md` name in `folder` that no file has taken yet: `stem.md`, then
    `stem (2).md`, `stem (3).md`... (D-73)."""
    target = folder / f"{stem}.md"
    n = 2
    while target.exists():
        target = folder / f"{stem} ({n}).md"
        n += 1
    return target


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
