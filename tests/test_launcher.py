"""`study_dashboard` (D-289): open the dashboard, start it when it is down, ask once about the
start at sign-in (Windows, and macOS by D-331). `study stop` and `study start` (D-342). No real
process, browser, registry or home folder: each test swaps those seams for a record."""

import http.server
import importlib.metadata
import json
import os
import plistlib
import subprocess
import sys
import threading

import pytest
from click.testing import CliRunner

from studysystem.cli import study
from studysystem.services import launcher, private_view

VERSION = importlib.metadata.version("studysystem")


@pytest.fixture
def machine(monkeypatch):
    """A fake machine: whether a dashboard answers, its version, what was spawned, asked to stop and
    opened, the Run key."""
    state = {
        "up": False,
        "version": VERSION,
        "listening": None,  # None: the same as "up"
        "looks_until_gone": 0,  # how many looks the asked process still runs for
        "spawned": [],
        "stopped": [],
        "opened": [],
        "run": None,
        "up_after_spawn": True,
    }

    def spawn(port):
        state["spawned"].append(port)
        state["up"], state["version"] = state["up_after_spawn"], VERSION

    def ask_to_stop(port):
        state["stopped"].append(port)
        state["up"] = False
        return 4321  # its process id

    def runs(pid):
        assert pid == 4321
        state["looks_until_gone"] -= 1
        return state["looks_until_gone"] >= 0

    def listening(port=launcher.PORT):
        return state["up"] if state["listening"] is None else state["listening"]

    def set_run(on):
        state["run"] = launcher._autostart_command() if on else None

    monkeypatch.setattr(
        launcher,
        "running_version",
        lambda port=launcher.PORT: state["version"] if state["up"] else None,
    )
    monkeypatch.setattr(launcher, "_listening", listening)
    monkeypatch.setattr(launcher, "_ask_to_stop", ask_to_stop)
    monkeypatch.setattr(launcher, "_runs", runs)
    monkeypatch.setattr(launcher, "_spawn", spawn)
    monkeypatch.setattr(launcher.webbrowser, "open", lambda u: state["opened"].append(u) or True)
    monkeypatch.setattr(launcher.sys, "platform", "win32")
    monkeypatch.setattr(launcher, "autostart_enabled", lambda: state["run"] is not None)
    monkeypatch.setattr(launcher, "_set_autostart", set_run)
    monkeypatch.setattr(launcher.time, "sleep", lambda s: None)
    return state


def test_a_running_dashboard_is_only_opened(machine):
    machine["up"] = True

    sent = launcher.dashboard("open")

    assert machine["spawned"] == []
    assert machine["opened"] == ["http://127.0.0.1:8765/"]
    assert sent["started"] is False and sent["url"] == "http://127.0.0.1:8765/"


def test_a_stopped_dashboard_is_started_then_opened(machine):
    sent = launcher.dashboard()  # open is the default

    assert machine["spawned"] == [8765]
    assert machine["opened"] == ["http://127.0.0.1:8765/"]
    assert sent["started"] is True and sent["browser_opened"] is True


def test_a_dashboard_that_never_answers_is_refused_with_the_log(machine, monkeypatch):
    machine["up_after_spawn"] = False
    clock = iter(range(0, 1000, 5))  # 5 s per look, past WAIT_SECONDS by the fourth
    monkeypatch.setattr(launcher.time, "monotonic", lambda: next(clock))

    with pytest.raises(launcher.StudyError) as err:
        launcher.dashboard("open")

    assert err.value.code == "dashboard_not_started"
    assert "dashboard.log" in err.value.fix
    assert machine["opened"] == []


def test_the_first_open_asks_about_the_windows_start(machine):
    machine["up"] = True

    assert launcher.dashboard("open")["ask_user"] == launcher.ASK


def test_an_answer_of_no_stops_the_question(machine, monkeypatch):
    monkeypatch.delenv("STUDYSYSTEM_DATA_DIR")  # the default folder, as a real install has
    machine["up"] = True

    off = launcher.dashboard("autostart_off")

    assert off == {"autostart": False, "command": None}
    sent = launcher.dashboard("open")
    assert (sent["autostart"], sent["ask_user"]) == (False, None)


@pytest.mark.parametrize("action", ["autostart_on", "autostart_off"])
@pytest.mark.parametrize("was", [None, "study dashboard"])
def test_inside_claude_the_start_is_refused_and_nothing_changes(machine, monkeypatch, action, was):
    """Claude's copy of the Run key would change, and Windows would start nothing (D-358)."""
    monkeypatch.delenv("STUDYSYSTEM_DATA_DIR")  # the default folder, as a real install has
    monkeypatch.setattr(private_view, "active", lambda: True)
    machine["run"] = was

    with pytest.raises(launcher.StudyError) as err:
        launcher.dashboard(action)

    assert err.value.code == "autostart_private_view"
    assert "study setup" in err.value.fix and "outside Claude" in err.value.fix
    assert machine["run"] == was
    assert not launcher._settings_path().exists()


@pytest.mark.parametrize("seen", [None, "study dashboard"])
def test_inside_claude_open_works_and_does_not_ask(machine, monkeypatch, seen):
    """The start that Claude sees is not the real one, so it is unknown (D-358)."""
    monkeypatch.setattr(private_view, "active", lambda: True)
    machine["run"] = seen

    sent = launcher.dashboard("open")

    assert machine["opened"] == ["http://127.0.0.1:8765/"]
    assert (sent["autostart"], sent["ask_user"]) == (None, None)


def test_an_answer_of_yes_writes_the_windows_start(machine, monkeypatch):
    monkeypatch.delenv("STUDYSYSTEM_DATA_DIR")  # the default folder, as a real install has
    machine["up"] = True

    on = launcher.dashboard("autostart_on")

    assert on["autostart"] is True
    assert machine["run"] == on["command"]
    assert "-m studysystem.cli dashboard --port 8765" in on["command"]
    sent = launcher.dashboard("open")
    assert (sent["autostart"], sent["ask_user"]) == (True, None)


def test_the_answer_is_kept_in_the_data_folder(machine, monkeypatch, tmp_path):
    monkeypatch.delenv("STUDYSYSTEM_DATA_DIR")

    launcher.dashboard("autostart_off")

    # the suite's stand-in for the platform folder, so no test writes the real one
    kept = tmp_path / "default" / "studysystem" / "dashboard.json"
    assert json.loads(kept.read_text(encoding="utf-8")) == {"autostart_asked": True}


@pytest.mark.parametrize("action", ["autostart_on", "autostart_off"])
def test_a_custom_data_folder_changes_no_windows_start(machine, isolated_data_dir, action):
    """STUDYSYSTEM_DATA_DIR is set by the autouse fixture. Windows starts the dashboard on the
    default folder, so the start belongs to the server of that folder, on and off (D-340)."""
    machine["run"] = "his own start"

    with pytest.raises(launcher.StudyError) as err:
        launcher.dashboard(action)

    assert err.value.code == "autostart_custom_data_dir"
    assert machine["run"] == "his own start"
    assert not (isolated_data_dir / "dashboard.json").exists()


def test_on_linux_the_start_is_refused_and_never_asked(machine, monkeypatch):
    monkeypatch.setattr(launcher.sys, "platform", "linux")
    monkeypatch.setattr(launcher, "autostart_enabled", lambda: None)
    machine["up"] = True

    with pytest.raises(launcher.StudyError) as err:
        launcher.dashboard("autostart_on")
    sent = launcher.dashboard("open")

    assert err.value.code == "autostart_unsupported"
    assert (sent["autostart"], sent["ask_user"]) == (None, None)


def test_an_unknown_action_names_the_three(machine):
    with pytest.raises(launcher.StudyError) as err:
        launcher.dashboard("restart")

    assert err.value.code == "invalid_value"
    assert "open, autostart_on, autostart_off" in err.value.fix


def test_the_dashboard_runs_with_this_servers_own_python():
    cmd = launcher.command(9000)

    assert cmd[1:] == ["-m", "studysystem.cli", "dashboard", "--port", "9000"]
    assert cmd[0].lower().startswith(str(launcher.Path(sys.executable).parent).lower())


def test_the_tool_answers_in_the_structured_shape(machine, call):
    machine["up"] = True

    result = call("study_dashboard", {"action": "open"})

    assert not result.is_error
    assert result.structured_content["url"] == "http://127.0.0.1:8765/"


def test_nothing_answers_on_a_free_port():
    assert launcher.answering(1) is False  # port 1: nothing listens there


# --- the macOS start: a LaunchAgent (D-331) ------------------------------------------------------


@pytest.fixture
def mac(monkeypatch, tmp_path):
    """A Mac with the default data folder and a home of its own. Returns where the agent goes."""
    monkeypatch.delenv("STUDYSYSTEM_DATA_DIR")
    monkeypatch.setattr(launcher.sys, "platform", "darwin")
    monkeypatch.setattr(launcher.Path, "home", lambda: tmp_path / "home")
    return tmp_path / "home" / "Library" / "LaunchAgents" / "studysystem.dashboard.plist"


def test_on_a_mac_yes_writes_the_launch_agent(mac):
    assert launcher.autostart_enabled() is False

    on = launcher.dashboard("autostart_on")

    agent = plistlib.loads(mac.read_bytes())
    assert agent["Label"] == "studysystem.dashboard"
    assert agent["RunAtLoad"] is True
    assert agent["ProgramArguments"] == launcher.command()
    assert "KeepAlive" not in agent  # it would undo `study stop`
    assert agent["StandardErrorPath"] == str(launcher.data_dir() / "dashboard.log")
    assert on["autostart"] is True and "dashboard --port 8765" in on["command"]
    assert launcher.autostart_enabled() is True


def test_on_a_mac_no_removes_the_launch_agent(mac):
    launcher.dashboard("autostart_on")

    off = launcher.dashboard("autostart_off")

    assert off == {"autostart": False, "command": None}
    assert not mac.exists() and launcher.autostart_enabled() is False
    launcher.dashboard("autostart_off")  # already off: nothing to remove


@pytest.mark.parametrize("action", ["autostart_on", "autostart_off"])
def test_on_a_mac_a_custom_data_folder_changes_no_start(mac, monkeypatch, tmp_path, action):
    launcher.dashboard("autostart_on")  # the default folder's own start
    before = mac.read_bytes()
    monkeypatch.setenv("STUDYSYSTEM_DATA_DIR", str(tmp_path / "data"))

    with pytest.raises(launcher.StudyError) as err:
        launcher.dashboard(action)

    assert err.value.code == "autostart_custom_data_dir"
    assert mac.read_bytes() == before


# --- `study stop` and `study start` (D-342) ------------------------------------------------------


def test_stop_asks_the_dashboard_and_waits_for_it(machine):
    machine["up"] = True

    assert launcher.stop_dashboard() == {"stopped": True}
    assert machine["stopped"] == [8765]


def test_stop_with_no_dashboard_is_no_error(machine):
    assert launcher.stop_dashboard() == {"stopped": False}
    assert machine["stopped"] == []


def test_stop_never_asks_a_program_that_is_not_the_dashboard(machine):
    machine["listening"] = True  # it holds the port, but its /health is not ours

    with pytest.raises(launcher.StudyError) as err:
        launcher.stop_dashboard()

    assert err.value.code == "dashboard_not_stopped"
    assert machine["stopped"] == []


def test_stop_waits_until_the_process_has_ended(machine):
    """The port is free before the process ends, and an upgrade needs the process gone (D-343)."""
    machine["up"], machine["looks_until_gone"] = True, 3

    assert launcher.stop_dashboard() == {"stopped": True}
    assert machine["looks_until_gone"] == -1  # three looks saw it, the fourth did not


def test_stop_is_refused_when_the_process_never_ends(machine, monkeypatch):
    machine["up"], machine["looks_until_gone"] = True, 1000
    clock = iter(range(0, 1000, 5))
    monkeypatch.setattr(launcher.time, "monotonic", lambda: next(clock))

    with pytest.raises(launcher.StudyError) as err:
        launcher.stop_dashboard()

    assert err.value.code == "dashboard_not_stopped"
    assert "still runs" in err.value.message


@pytest.mark.skipif(sys.platform != "win32", reason="the wait for the process is Windows only")
def test_a_process_is_seen_until_it_has_ended():
    child = subprocess.Popen(
        [sys.executable, "-c", "import os; print(os.getpid(), flush=True); input()"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        pid = int(child.stdout.readline())  # a virtual environment's python runs in a child of it
        assert launcher._runs(pid) is True
    finally:
        child.communicate("\n")

    assert launcher._runs(pid) is False
    assert launcher._runs(None) is False  # a reply that names no process


def test_off_windows_no_process_is_waited_for(monkeypatch):
    """macOS lets an upgrade replace a running program's files, and a dashboard that a host started
    stays in the process list until the host collects it (D-343)."""
    monkeypatch.setattr(launcher.sys, "platform", "darwin")

    assert launcher._runs(os.getpid()) is False


def test_stop_is_refused_when_the_dashboard_stays_up(machine, monkeypatch):
    machine["up"] = True
    monkeypatch.setattr(launcher, "_ask_to_stop", lambda port: 4321)
    clock = iter(range(0, 1000, 5))
    monkeypatch.setattr(launcher.time, "monotonic", lambda: next(clock))

    with pytest.raises(launcher.StudyError) as err:
        launcher.stop_dashboard()

    assert err.value.code == "dashboard_not_stopped"


def test_start_starts_it_and_opens_no_browser(machine):
    assert launcher.start_dashboard() == {"url": "http://127.0.0.1:8765/", "started": True}
    assert machine["spawned"] == [8765] and machine["opened"] == []


def test_start_leaves_the_same_version_running(machine):
    machine["up"] = True

    assert launcher.start_dashboard()["started"] is False
    assert (machine["spawned"], machine["stopped"]) == ([], [])


def test_start_replaces_a_dashboard_of_another_version(machine):
    """He forgot `study stop` before the upgrade: the old code still answers."""
    machine["up"], machine["version"] = True, "0.0.1"

    assert launcher.start_dashboard()["started"] is True
    assert (machine["stopped"], machine["spawned"]) == ([8765], [8765])


def test_start_is_refused_when_the_port_is_held(machine):
    """A dashboard too busy to answer, or another program: a second one could not take the port,
    and the first one answering again would read as started."""
    machine["listening"] = True

    with pytest.raises(launcher.StudyError) as err:
        launcher.start_dashboard()

    assert err.value.code == "dashboard_not_stopped"
    assert "busy" in err.value.fix
    assert (machine["spawned"], machine["stopped"]) == ([], [])


def test_open_keeps_a_dashboard_of_another_version(machine):
    """Two installs on one machine must not keep replacing each other's dashboard (D-342)."""
    machine["up"], machine["version"] = True, "0.0.1"

    assert launcher.dashboard("open")["started"] is False
    assert machine["stopped"] == []


@pytest.fixture
def fake_dashboard():
    """A real socket on a free port that answers like a dashboard; `shutdown` is its reply code to
    POST /shutdown, and 200 takes it down; `reply` is that reply's body."""
    seen = {"posts": [], "shutdown": 200, "reply": {"stopping": True, "pid": 4321}}

    class Handler(http.server.BaseHTTPRequestHandler):
        def _send(self, code, body):
            self.send_response(code)
            self.end_headers()
            self.wfile.write(json.dumps(body).encode())

        def do_GET(self):
            self._send(200, {"server_version": "0.0.1", "schema_revision": None})

        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            seen["posts"].append((self.path, self.headers["Content-Type"], body))
            self._send(seen["shutdown"], seen["reply"])
            if seen["shutdown"] == 200:
                threading.Thread(target=down).start()

        def log_message(self, *args):
            pass

    server = http.server.HTTPServer(("127.0.0.1", 0), Handler)

    def down():
        server.shutdown()
        server.server_close()

    threading.Thread(target=server.serve_forever, daemon=True).start()
    seen["port"] = server.server_address[1]
    yield seen
    down()


def test_stop_posts_json_to_the_dashboards_shutdown(fake_dashboard, monkeypatch):
    port = fake_dashboard["port"]
    looked = []
    monkeypatch.setattr(launcher, "_runs", lambda pid: looked.append(pid))
    assert launcher.running_version(port) == "0.0.1"

    assert launcher.stop_dashboard(port) == {"stopped": True}

    assert looked == [4321]  # the process that the reply named
    assert fake_dashboard["posts"] == [("/shutdown", "application/json", b"{}")]
    assert launcher.answering(port) is False


@pytest.mark.parametrize("reply", [{}, [], {"pid": "4321"}, {"pid": 1.5}, {"pid": None}])
def test_stop_without_a_process_id_waits_for_the_port(fake_dashboard, reply):
    fake_dashboard["reply"] = reply

    assert launcher.stop_dashboard(fake_dashboard["port"]) == {"stopped": True}


def test_stop_names_the_way_out_for_an_older_dashboard(fake_dashboard):
    """One from before D-342 has no /shutdown."""
    fake_dashboard["shutdown"] = 404

    with pytest.raises(launcher.StudyError) as err:
        launcher.stop_dashboard(fake_dashboard["port"])

    assert err.value.code == "dashboard_not_stopped"
    assert "sign out" in err.value.fix


def test_the_two_commands_print_one_line(machine):
    started = CliRunner().invoke(study, ["start"])
    again = CliRunner().invoke(study, ["start"])
    stopped = CliRunner().invoke(study, ["stop"])
    nothing = CliRunner().invoke(study, ["stop"])

    assert [r.exit_code for r in (started, again, stopped, nothing)] == [0, 0, 0, 0]
    assert started.output == "dashboard started: http://127.0.0.1:8765/\n"
    assert again.output == "dashboard already running: http://127.0.0.1:8765/\n"
    assert stopped.output == "dashboard stopped\n"
    assert nothing.output == "no dashboard was running\n"


def test_a_refused_stop_is_one_line_and_exit_1(machine):
    machine["listening"] = True

    refused = CliRunner().invoke(study, ["stop"])

    assert refused.exit_code == 1
    assert refused.output.startswith("Error: dashboard_not_stopped: ")
