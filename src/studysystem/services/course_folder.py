"""The folder a course's notes and summaries go in, under the output folder (D-346 to D-348).

A declared `course.vault_folder` is used as it is and never created: a typo must not grow a second
folder beside the real one. A course with none gets `<semester>/<code> - <name>`, created at its
first export and saved in `vault_folder`.
"""

from pathlib import Path

from sqlalchemy import Engine, select, update

from studysystem.db.tables import course
from studysystem.services import settings, values
from studysystem.services.units import read_unit, write_unit


class NotReady(Exception):
    """The folder cannot be used: what is wrong, and how to fix it."""

    def __init__(self, problem: str, fix: str):
        super().__init__(problem)
        self.problem = problem
        self.fix = fix


def _part(text: str) -> str:
    """The text as one folder name."""
    out = values.WINDOWS_REFUSED.sub("-", text).rstrip(" .").lstrip()
    return f"_{out}" if not out or values.WINDOWS_RESERVED.fullmatch(out) else out


def own_name(code: str, name: str, semester_name: str) -> str:
    """The folder the server gives a course, relative to the output folder, with `/`."""
    return f"{_part(semester_name)}/{_part(f'{code} - {name}')}"


def ready(engine: Engine, course_id: str) -> tuple[Path, str]:
    """The output folder and the course's folder under it, both there. Creates and saves the
    course's folder when the server is the one to name it.

    Raises NotReady, or the OSError or SQLAlchemyError of the create or the save.
    """
    root = settings.output_dir()
    if root is None:
        raise NotReady("this machine has no output folder set", "run `study setup` in a terminal")
    if not root.is_dir():  # never created (D-352): an unplugged drive must not grow a new one
        raise NotReady(
            f"the output folder {root} is not a folder on this computer",
            "create it, or run `study setup` in a terminal to choose another one",
        )
    with read_unit(engine) as conn:
        found = conn.execute(
            select(
                course.c.code, course.c.name, course.c.semester_name, course.c.vault_folder
            ).where(course.c.id == course_id)
        ).one()
    own = own_name(found.code, found.name, found.semester_name)
    folder = found.vault_folder
    if folder is None or folder == own:
        (root / own).mkdir(parents=True, exist_ok=True)
    if folder is None:
        folder = _save(engine, course_id, own)
    if not (root / folder).is_dir():
        raise NotReady(
            f"{folder} is not a folder under the output folder {root}",
            "create that folder, or give a valid one with "
            "study_set_course_input(field='vault_folder')",
        )
    return root, folder


def _save(engine: Engine, course_id: str, own: str) -> str:
    """Save the folder the server named, unless one was declared meanwhile; the one that stands."""
    with write_unit(engine) as conn:
        conn.execute(
            update(course)
            .where(course.c.id == course_id, course.c.vault_folder.is_(None))
            .values(vault_folder=own)
        )
        return conn.execute(
            select(course.c.vault_folder).where(course.c.id == course_id)
        ).scalar_one()
