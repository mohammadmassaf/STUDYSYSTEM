"""Open the dashboard from the host, turn its start at sign-in on or off (D-289), and stop and
start it around an upgrade (D-342).

The dashboard is a process of its own (D-17, D-19). `open_dashboard` starts it when nothing answers
on 127.0.0.1, waits for it, and opens the browser there. The start at sign-in needs no admin rights
and shows no window. On Windows it is a value under the user's own Run key: `pythonw` runs the same
`study dashboard`. On macOS it is a LaunchAgent, a small file that macOS reads at sign-in (D-331).

Start at sign-in is the default he wants, but it is asked, never set silently: until he answers
once, `open_dashboard`'s reply carries the question for the host. His answer is kept in
`dashboard.json` in the data folder - a setting of this machine, not a row of the database.

Nothing here reads or writes the database.
"""

import http.client
import importlib.metadata
import json
import os
import plistlib
import shlex
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
import webbrowser
from collections.abc import Callable
from pathlib import Path

from studysystem.db.engine import data_dir
from studysystem.errors import StudyError
from studysystem.services import values

HOST = "127.0.0.1"
PORT = 8765  # `study dashboard`'s default
ACTIONS = ("open", "autostart_on", "autostart_off")
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_VALUE = "StudySystemDashboard"
AGENT_LABEL = "studysystem.dashboard"
WAIT_SECONDS = 15  # a cold start imports the web stack and opens the database
ASK = (
    "Start the study dashboard every time you sign in to this computer? It then runs in the "
    "background, so the stopwatch always works. You can change this anytime."
)


def url(port: int = PORT) -> str:
    return f"http://{HOST}:{port}/"


def running_version(port: int = PORT) -> str | None:
    """The version of the study dashboard that answers on the port, as its /health names it; None
    when none answers."""
    try:
        with urllib.request.urlopen(url(port) + "health", timeout=1) as reply:
            return json.load(reply)["server_version"] if reply.status == 200 else None
    # refused, timed out, not HTTP, or not our JSON
    except (OSError, http.client.HTTPException, ValueError, KeyError, TypeError):
        return None


def answering(port: int = PORT) -> bool:
    """Whether a study dashboard, of any version, answers on the port."""
    return running_version(port) is not None


def _listening(port: int) -> bool:
    """Whether any program holds the port: a dashboard too busy to answer /health still does."""
    try:
        socket.create_connection((HOST, port), timeout=1).close()
        return True
    except OSError:
        return False


def _wait_until(true: Callable[[], bool]) -> bool:
    """Look until it is true, for WAIT_SECONDS at most; whether it became true."""
    deadline = time.monotonic() + WAIT_SECONDS
    while not true():
        if time.monotonic() > deadline:
            return False
        time.sleep(0.3)
    return True


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


def _start(port: int) -> None:
    """Spawn it and wait for it. Refused: dashboard_not_started - nothing answered within
    WAIT_SECONDS; the fix names the log."""
    _spawn(port)
    if not _wait_until(lambda: answering(port)):
        raise StudyError(
            code="dashboard_not_started",
            message=f"the dashboard did not answer on {url(port)} after {WAIT_SECONDS}s",
            fix=f"read {data_dir() / 'dashboard.log'} for the reason, or run "
            "`study dashboard` in a terminal to see it",
        )


def _ask_to_stop(port: int) -> int | None:
    """Ask the dashboard to stop. Returns its process id; None when the reply has no usable one
    (D-343): no reply of ours, no id, or an id that is not a whole number."""
    ask = urllib.request.Request(
        url(port) + "shutdown", data=b"{}", headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(ask, timeout=5) as reply:
        try:
            pid = json.load(reply).get("pid")
        except (ValueError, AttributeError):  # not our JSON
            return None
    return pid if isinstance(pid, int) else None


def _runs(pid: int | None) -> bool:
    """Whether the process still runs, which on Windows keeps an upgrade from replacing the
    install's files. Always false elsewhere: there they can be replaced, and a dashboard that a host
    started stays in the process list after it ends, until the host collects it (D-343)."""
    if pid is None or sys.platform != "win32":
        return False
    import _winapi

    try:
        process = _winapi.OpenProcess(_winapi.SYNCHRONIZE, False, pid)
    except OSError:
        return False  # it has ended
    try:
        return _winapi.WaitForSingleObject(process, 0) == _winapi.WAIT_TIMEOUT
    finally:
        _winapi.CloseHandle(process)


# --- the start at sign-in: a value under HKCU's Run key, or a LaunchAgent -------------------------


def _unsupported() -> StudyError:
    return StudyError(
        code="autostart_unsupported",
        message="starting the dashboard at sign-in works on Windows and macOS only",
        fix="call study_dashboard with action 'open' whenever the dashboard is needed",
    )


def _autostart_command() -> str:
    join = shlex.join if sys.platform == "darwin" else subprocess.list2cmdline
    return join(command())


def _agent_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"


def _agent() -> bytes:
    """The LaunchAgent's file. No KeepAlive: macOS would start it again right after `study stop`."""
    return plistlib.dumps(
        {
            "Label": AGENT_LABEL,
            "ProgramArguments": command(),
            "RunAtLoad": True,
            "StandardErrorPath": str(data_dir() / "dashboard.log"),
        }
    )


def autostart_enabled() -> bool | None:
    """Whether the dashboard starts at sign-in; None on a system where it cannot."""
    if sys.platform == "darwin":
        return _agent_path().exists()
    if sys.platform != "win32":
        return None
    import winreg

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            winreg.QueryValueEx(key, RUN_VALUE)
            return True
    except FileNotFoundError:
        return False


def _set_agent(on: bool) -> None:
    path = _agent_path()
    if on:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(_agent())
    else:
        path.unlink(missing_ok=True)


def _set_autostart(on: bool) -> None:
    if sys.platform == "darwin":
        _set_agent(on)
        return
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
    (true, false, or null where there is no start at sign-in), "ask_user" (the question for the
    host, or null)}. A dashboard of another version counts as running: two installs on one machine
    must not keep replacing each other's (D-342).

    Refused: dashboard_not_started, as `_start` says.
    """
    started = not answering(port)
    if started:
        _start(port)
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
    """Turn the start at sign-in on or off, and keep his answer so the question is not asked again.

    Returns {"autostart", "command"} - the command the computer runs, or null when off.

    Refused: autostart_unsupported off Windows and macOS; autostart_custom_data_dir while
    STUDYSYSTEM_DATA_DIR points elsewhere - the one start on this machine is for the default folder,
    so a server on another one must not set it, nor remove it (D-340).
    """
    if sys.platform not in ("win32", "darwin"):
        raise _unsupported()
    if os.environ.get("STUDYSYSTEM_DATA_DIR"):
        raise StudyError(
            code="autostart_custom_data_dir",
            message="this server uses a custom data folder; the start at sign-in uses the default",
            fix="turn it on or off from a server that uses the default data folder",
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


# --- `study stop` and `study start`, around an upgrade (D-342) ------------------------------------


def _not_stopped(
    port: int,
    why: str,
    # The sign-out comes after the upgrade: the start at sign-in would start the old version again.
    fix: str = "run the upgrade if it has not run yet, then sign out of the computer and in again: "
    "the new version starts then",
) -> StudyError:
    return StudyError(code="dashboard_not_stopped", message=f"{url(port)} {why}", fix=fix)


def stop_dashboard(port: int = PORT) -> dict:
    """Ask the dashboard to stop, and wait until it has let the port go and, on Windows, its process
    has ended. Returns {"stopped"}: false when none was running.

    Refused: dashboard_not_stopped - another program holds the port (it is never asked), the
    dashboard is older than D-342, or it did not stop within WAIT_SECONDS.
    """
    if not answering(port):
        if _listening(port):
            raise _not_stopped(
                port,
                "is held by a program that does not answer as the dashboard",
                "close that program, or wait if the dashboard is busy, then run the command again",
            )
        return {"stopped": False}
    pid = None
    try:
        pid = _ask_to_stop(port)
    except urllib.error.HTTPError:
        raise _not_stopped(port, "is a dashboard too old to be stopped this way") from None
    except (OSError, http.client.HTTPException):
        pass  # it may have gone before it replied; the wait decides
    if not _wait_until(lambda: not _listening(port) and not _runs(pid)):
        raise _not_stopped(port, f"still runs {WAIT_SECONDS}s after it was asked to stop")
    return {"stopped": True}


def start_dashboard(port: int = PORT) -> dict:
    """Start the dashboard with no window and no browser. One of another version is stopped first:
    it is the old code, left running through an upgrade. Returns {"url", "started"}.

    Refused: dashboard_not_stopped and dashboard_not_started, as above.
    """
    if running_version(port) == importlib.metadata.version("studysystem"):
        return {"url": url(port), "started": False}
    stop_dashboard(port)  # also refuses a port that is held but does not answer
    _start(port)
    return {"url": url(port), "started": True}
