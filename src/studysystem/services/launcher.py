"""Open the dashboard from the host, and turn its Windows start on or off (D-289).

The dashboard is a process of its own (D-17, D-19). `open_dashboard` starts it when nothing answers
on 127.0.0.1, waits for it, and opens the browser there. The Windows start is a value under the
user's own Run key - no admin rights, no window: `pythonw` runs the same `study dashboard`.

Start with Windows is the default he wants, but it is asked, never set silently: until he answers
once, `open_dashboard`'s reply carries the question for the host. His answer is kept in
`dashboard.json` in the data folder - a setting of this machine, not a row of the database.

Nothing here reads or writes the database.
"""

import json
import os
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

from studysystem.db.engine import data_dir
from studysystem.errors import StudyError
from studysystem.services import values

HOST = "127.0.0.1"
PORT = 8765  # `study dashboard`'s default
ACTIONS = ("open", "autostart_on", "autostart_off")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "StudySystemDashboard"
WAIT_SECONDS = 15  # a cold start imports the web stack and opens the database
ASK = (
    "Start the study dashboard every time Windows starts? It then runs in the background, so the "
    "stopwatch always works. You can change this anytime."
)


def url(port: int = PORT) -> str:
    return f"http://{HOST}:{port}/"


def answering(port: int = PORT) -> bool:
    """Whether a study dashboard answers on the port: its /health names a server version."""
    try:
        with urllib.request.urlopen(url(port) + "health", timeout=1) as reply:
            return reply.status == 200 and "server_version" in json.load(reply)
    except (OSError, ValueError):  # refused, timed out, or not our JSON
        return False


def _pythonw() -> str:
    """The interpreter that shows no window on Windows, beside this one; else this one."""
    beside = Path(sys.executable).with_name("pythonw.exe")
    return str(beside) if sys.platform == "win32" and beside.exists() else sys.executable


def command(port: int = PORT) -> list[str]:
    """`study dashboard` with this server's own interpreter, so it runs the same code."""
    return [_pythonw(), "-m", "studysystem.cli", "dashboard", "--port", str(port)]


def _spawn(port: int) -> None:
    """Start the dashboard detached from this process: it must outlive the host's server, and it
    must not hold the host's stdio, which carries the MCP messages. Its output goes to a log."""
    log = (data_dir() / "dashboard.log").open("ab")
    flags = {}
    if sys.platform == "win32":
        flags["creationflags"] = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
    else:
        flags["start_new_session"] = True
    with log:
        subprocess.Popen(
            command(port), stdin=subprocess.DEVNULL, stdout=log, stderr=log, cwd=data_dir(), **flags
        )


# --- the Windows start: one value under HKCU's Run key -------------------------------------------


def _unsupported() -> StudyError:
    return StudyError(
        code="autostart_unsupported",
        message="starting the dashboard with the computer works on Windows only",
        fix="call study_dashboard with action 'open' whenever the dashboard is needed",
    )


def _autostart_command() -> str:
    return subprocess.list2cmdline(command())


def autostart_enabled() -> bool | None:
    """Whether Windows starts the dashboard at login; None off Windows, where it cannot."""
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_VALUE)
            return True
    except FileNotFoundError:
        return False


def _set_autostart(on: bool) -> None:
    import winreg

    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if on:
            winreg.SetValueEx(key, RUN_VALUE, 0, winreg.REG_SZ, _autostart_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_VALUE)
            except FileNotFoundError:
                pass  # already off


# --- whether he answered the question ------------------------------------------------------------


def _settings_path() -> Path:
    return data_dir() / "dashboard.json"


def _asked() -> bool:
    try:
        return bool(json.loads(_settings_path().read_text(encoding="utf-8")).get("autostart_asked"))
    except (OSError, ValueError, AttributeError):  # no file yet, or one we cannot read
        return False


def _mark_asked() -> None:
    _settings_path().write_text(json.dumps({"autostart_asked": True}), encoding="utf-8")


# --- the tool's three actions --------------------------------------------------------------------


def open_dashboard(port: int = PORT) -> dict:
    """Start the dashboard if it does not answer, wait for it, open the browser.

    Returns {"url", "started" (true when this call started it), "browser_opened", "autostart"
    (true, false, or null off Windows), "ask_user" (the question for the host, or null)}.

    Refused: dashboard_not_started - nothing answered within WAIT_SECONDS; the fix names the log.
    """
    started = False
    if not answering(port):
        _spawn(port)
        started = True
        deadline = time.monotonic() + WAIT_SECONDS
        while not answering(port):
            if time.monotonic() > deadline:
                raise StudyError(
                    code="dashboard_not_started",
                    message=f"the dashboard did not answer on {url(port)} after {WAIT_SECONDS}s",
                    fix=f"read {data_dir() / 'dashboard.log'} for the reason, or run "
                    "`study dashboard` in a terminal to see it",
                )
            time.sleep(0.3)
    enabled = autostart_enabled()
    ask = enabled is False and not _asked()
    return {
        "url": url(port),
        "started": started,
        "browser_opened": webbrowser.open(url(port)),
        "autostart": enabled,
        "ask_user": ASK if ask else None,
    }


def set_autostart(on: bool) -> dict:
    """Turn the Windows start on or off, and keep his answer so the question is not asked again.

    Returns {"autostart", "command"} - the command Windows runs, or null when off.

    Refused: autostart_unsupported off Windows; autostart_custom_data_dir on, while
    STUDYSYSTEM_DATA_DIR points elsewhere - Windows would start a dashboard on the default folder.
    """
    if sys.platform != "win32":
        raise _unsupported()
    if on and os.environ.get("STUDYSYSTEM_DATA_DIR"):
        raise StudyError(
            code="autostart_custom_data_dir",
            message="this server uses a custom data folder; a Windows start would use the default",
            fix="turn the Windows start on from a server that uses the default data folder",
        )
    _set_autostart(on)
    _mark_asked()
    return {"autostart": on, "command": _autostart_command() if on else None}


def dashboard(action: object = "open") -> dict:
    """The tool's entry: one of ACTIONS."""
    action = values.choice("action", action, ACTIONS)
    if action == "open":
        return open_dashboard()
    return set_autostart(action == "autostart_on")
