"""What `study setup` does to this machine (D-332, D-344 to D-356): the output folder's checks, and
the server entry in Claude Desktop's config. The questions are asked in `cli.py`.

Claude rewrites its config when it quits, so `write_entry` is called only after the student says it
is closed (D-345). It reads the file again then, and changes only `mcpServers.studysystem`.

Nothing here reads or writes the database.
"""

import datetime
import json
import os
import re
import shutil
import sys
from pathlib import Path

from studysystem.errors import StudyError
from studysystem.services import private_view, programs
from studysystem.services.settings import VAULT_ENV

DATA_ENV = "STUDYSYSTEM_DATA_DIR"
SERVER = "studysystem"
CONFIG = "claude_desktop_config.json"
LONG_PATH = 247  # characters: Windows' usual limit for a folder


def refuse_custom_data_dir() -> None:
    """Refused: setup_custom_data_dir while STUDYSYSTEM_DATA_DIR is set (D-354) - Claude's server
    would not see the variable, and would look for the saved folder elsewhere."""
    if os.environ.get(DATA_ENV):
        raise _custom_data_dir("this terminal", "unset the variable")


def _custom_data_dir(where: str, how: str) -> StudyError:
    return StudyError(
        code="setup_custom_data_dir",
        message=f"{where} sets {DATA_ENV}, a custom data folder; nothing was changed",
        fix=f"{how}, then run `study setup` again",
    )


def refuse_private_view() -> None:
    """Refused: setup_private_view when Windows would not see what setup changes (D-356)."""
    if private_view.active():
        raise StudyError(
            code="setup_private_view",
            message="this terminal runs inside Claude, where Windows does not see the changes; "
            "nothing was changed",
            fix="open a terminal from the Start menu (Windows PowerShell), then run `study setup`",
        )


# --- the output folder ---------------------------------------------------------------------------


def output_folder(text: str) -> Path:
    """The folder the student typed or pasted; Windows' "Copy as path" adds quotes.

    Refused: output_folder_invalid when it is not a full path to a folder that exists (D-352).
    """
    folder = Path(text.strip().strip("\"'").strip()).expanduser()
    if not folder.is_absolute() or not folder.is_dir():
        if len(str(folder)) > LONG_PATH:
            raise StudyError(
                code="output_folder_invalid",
                message=f"that path has {len(str(folder))} characters and leads to no folder on "
                "this computer; it may be too long",
                fix="paste a shorter path, to a folder that exists",
            )
        raise StudyError(
            code="output_folder_invalid",
            message=f"{folder} is not a folder on this computer",
            fix="create the folder, then paste its full path",
        )
    return folder


def same_folder(one: Path, other: Path) -> bool:
    return os.path.normcase(os.path.normpath(one)) == os.path.normcase(os.path.normpath(other))


# --- the server entry ----------------------------------------------------------------------------


def study_path() -> Path:
    """The full path of the `study` command that runs now: a program started from the macOS Dock
    does not find a command by its name (D-332). Windows hands it over without `.exe`.

    Refused: study_not_found when this is not the installed command.
    """
    name = "study.exe" if sys.platform == "win32" else "study"
    started = Path(sys.argv[0])
    named = started.is_absolute() and started.stem == "study"
    beside = [started.with_name(name)] if named else []
    for path in (*beside, Path(sys.executable).with_name(name)):
        if path.is_file():
            return path.absolute()
    found = programs.on_path("study")
    if found is None:
        raise StudyError(
            code="study_not_found",
            message="the installed `study` command was not found",
            fix="run `uv tool install studysystem`, then `study setup`",
        )
    return found


def entry() -> dict:
    """The server entry for every host that takes JSON."""
    return {"command": str(study_path()), "args": ["serve"]}


def codex_command() -> str:
    """The line that adds the server to Codex, whose config is TOML: the JSON entry pasted there
    stops Codex from starting (D-364)."""
    return f'codex mcp add {SERVER} -- "{study_path()}" serve'


def how_to_quit() -> str:
    """Closing Claude's window leaves it running (D-345)."""
    if sys.platform == "darwin":
        return "Quit Claude: click its window, then press Cmd+Q. Closing the window is not enough."
    return (
        "Quit Claude: find its icon in the tray (the small icons near the clock, some behind the "
        "^ arrow), right-click it, and choose Quit. Closing the window is not enough."
    )


# --- Claude Desktop's config ---------------------------------------------------------------------


def _places() -> list[Path]:
    """The folders Claude Desktop is known to keep its config in (D-344)."""
    home = Path.home()
    if sys.platform == "darwin":
        return [home / "Library" / "Application Support" / "Claude"]
    if sys.platform != "win32":
        return []
    local = private_view.local_app_data()
    roaming = Path(os.environ.get("APPDATA") or home / "AppData" / "Roaming")
    return [
        *(local / "Claude-Data").glob("*"),
        *(local / "Packages").glob("Claude_*/LocalCache/Roaming/Claude"),
        roaming / "Claude",
    ]


def config_files() -> list[Path]:
    """Every Claude Desktop config on this machine, the one written last first: Claude keeps
    rewriting the one it uses."""
    found = [place / CONFIG for place in _places() if (place / CONFIG).is_file()]
    return sorted(found, key=lambda path: path.stat().st_mtime, reverse=True)


def _unreadable(path: Path, why: str) -> StudyError:
    return StudyError(
        code="claude_config_unreadable",
        message=f"{path} {why}; it was not changed",
        fix="open Claude once so it repairs the file, quit it, then run `study setup` again",
    )


def _read(path: Path) -> dict:
    try:
        text = path.read_text(encoding="utf-8-sig")
        if not text.strip():
            raise _unreadable(path, "is empty")
        config = json.loads(text)
    except (OSError, ValueError) as e:
        raise _unreadable(path, f"could not be read: {e}") from None
    if not isinstance(config, dict) or not isinstance(config.get("mcpServers", {}), dict):
        raise _unreadable(path, "does not hold what Claude writes")
    return config


def current_entry(path: Path) -> dict | None:
    """The config's studysystem entry; None when it has none.

    Refused: claude_config_unreadable; setup_custom_data_dir when the entry names a data folder.
    """
    old = _read(path).get("mcpServers", {}).get(SERVER)
    if old is None:
        return None
    if not isinstance(old, dict) or not isinstance(old.get("env", {}), dict):
        raise _unreadable(path, f"holds a {SERVER} entry that is not one")
    if old.get("env", {}).get(DATA_ENV):
        raise _custom_data_dir(
            f"the {SERVER} entry in {path}", "quit Claude, remove that variable from the entry"
        )
    return old


def wanted_entry(old: dict | None) -> dict:
    """The entry setup would write: the old one's other variables stay, the output folder's goes,
    so the saved folder is the only one named (D-349)."""
    env = {k: v for k, v in (old or {}).get("env", {}).items() if k != VAULT_ENV}
    return entry() | ({"env": env} if env else {})


def write_entry(path: Path, new: dict) -> Path:
    """Set the studysystem entry and nothing else. Returns where the copy of the old file is.

    Refused: claude_config_unreadable; claude_config_not_written. In both the file is untouched.
    """
    config = _read(path)
    config["mcpServers"] = config.get("mcpServers", {}) | {SERVER: new}
    stamp = datetime.datetime.now().astimezone().strftime("%Y-%m-%d-%H%M%S")
    copy = path.with_name(f"{path.name}.bak-{stamp}")
    fresh = path.with_name(f"{path.name}.studysystem-new")
    try:
        shutil.copy2(path, copy)
        fresh.write_text(json.dumps(config, indent=2, ensure_ascii=False), encoding="utf-8")
        shutil.copymode(path, fresh)
        os.replace(fresh, path)  # whole or not at all
    except OSError as e:
        fresh.unlink(missing_ok=True)
        raise StudyError(
            code="claude_config_not_written",
            message=f"{path} could not be written: {e.strerror or e}; it was not changed",
            fix="quit Claude fully, then run `study setup` again",
        ) from None
    _drop_older_copies(path, copy)
    return copy


def _drop_older_copies(path: Path, keep: Path) -> None:
    """One copy is enough: each holds the API keys of the other servers in the config."""
    stamp = re.compile(re.escape(path.name) + r"\.bak-\d{4}-\d{2}-\d{2}-\d{6}")
    try:
        for old in path.parent.iterdir():
            if old != keep and stamp.fullmatch(old.name):
                old.unlink()
    except OSError:
        pass  # the config is written; a copy left behind is no failure
