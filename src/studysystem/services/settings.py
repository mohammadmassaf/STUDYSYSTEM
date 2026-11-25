"""The output folder: where the notes and summaries are written (D-346, D-350, D-351).

It is a setting of this machine, not a row: `settings.json` in the data folder, written only by
`study setup`. A file of its own, because the launcher writes `dashboard.json` whole.

Nothing here reads or writes the database.
"""

import json
import os
from pathlib import Path

from studysystem.db.engine import data_dir

VAULT_ENV = "STUDYSYSTEM_VAULT_DIR"
_KEY = "output_folder"


def _path() -> Path:
    return data_dir() / "settings.json"


def saved_output_dir() -> Path | None:
    """The folder `study setup` saved; None when nothing is saved, or the file is unreadable."""
    try:
        out = json.loads(_path().read_text(encoding="utf-8"))[_KEY]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return Path(out) if isinstance(out, str) and out else None


def variable_output_dir() -> Path | None:
    """The folder STUDYSYSTEM_VAULT_DIR names; None when it names none."""
    out = os.environ.get(VAULT_ENV, "").strip()
    return Path(out) if out else None


def output_dir() -> Path | None:
    """The folder every export goes under: the saved one, else - with no settings file - the
    variable's (D-351). A settings file that cannot be read is unknown, not empty: no folder then,
    so an old variable never takes over."""
    return saved_output_dir() if _path().exists() else variable_output_dir()


def save_output_dir(folder: Path) -> None:
    """Keep the folder, whole file at a time: a crash leaves the old file, never half of one."""
    path = _path()
    new = path.with_name(path.name + ".new")
    new.write_text(json.dumps({_KEY: str(folder)}), encoding="utf-8")
    os.replace(new, path)
