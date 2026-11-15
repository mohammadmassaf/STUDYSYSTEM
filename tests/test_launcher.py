"""`study_dashboard` (D-289): open the dashboard, start it when it is down, ask once about the
Windows start. No real process, browser or registry: each test swaps those seams for a record."""

import json
import sys

import pytest

from studysystem.services import launcher


@pytest.fixture
def machine(monkeypatch):
    """A fake machine: whether a dashboard answers, what was spawned and opened, the Run key."""
    state = {"up": False, "spawned": [], "opened": [], "run": None, "up_after_spawn": True}

    def spawn(port):
        state["spawned"].append(port)
        state["up"] = state["up_after_spawn"]

    def set_run(on):
        state["run"] = launcher._autostart_command() if on else None

    monkeypatch.setattr(launcher, "answering", lambda port=launcher.PORT: state["up"])
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


def test_an_answer_of_no_stops_the_question(machine):
    machine["up"] = True

    off = launcher.dashboard("autostart_off")

    assert off == {"autostart": False, "command": None}
    sent = launcher.dashboard("open")
    assert (sent["autostart"], sent["ask_user"]) == (False, None)


def test_an_answer_of_yes_writes_the_windows_start(machine, monkeypatch):
    monkeypatch.delenv("STUDYSYSTEM_DATA_DIR")  # the default folder, as a real install has
    machine["up"] = True

    on = launcher.dashboard("autostart_on")

    assert on["autostart"] is True
    assert machine["run"] == on["command"]
    assert "-m studysystem.cli dashboard --port 8765" in on["command"]
    sent = launcher.dashboard("open")
    assert (sent["autostart"], sent["ask_user"]) == (True, None)


def test_the_answer_is_kept_in_the_data_folder(machine, isolated_data_dir):
    launcher.dashboard("autostart_off")

    kept = json.loads((isolated_data_dir / "dashboard.json").read_text(encoding="utf-8"))

    assert kept == {"autostart_asked": True}


def test_a_custom_data_folder_refuses_the_windows_start(machine):
    # STUDYSYSTEM_DATA_DIR is set by the autouse fixture: Windows would start on the default one
    with pytest.raises(launcher.StudyError) as err:
        launcher.dashboard("autostart_on")

    assert err.value.code == "autostart_custom_data_dir"
    assert machine["run"] is None


def test_off_windows_the_start_is_refused_and_never_asked(machine, monkeypatch):
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
