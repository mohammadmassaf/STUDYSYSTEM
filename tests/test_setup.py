"""`study setup` (D-332, D-344 to D-356): the output folder, Claude's config, the start at sign-in.

No real config, registry, home folder or dashboard: each test has its own under tmp_path."""

import json
import os

import pytest
from click.testing import CliRunner

from studysystem.cli import study
from studysystem.errors import StudyError
from studysystem.services import launcher, private_view, settings
from studysystem.services import setup as machine

CHECK = private_view.active  # the real one: conftest replaces it in every test

NEW = {"command": "/bin/study", "args": ["serve"]}
# Mohammad's own shape: the repo's server, and the variable that used to win
OLD = {
    "command": "uv",
    "args": ["run", "--directory", "repo", "python", "-m", "studysystem.server"],
    "env": {"STUDYSYSTEM_VAULT_DIR": "old vault"},
}


@pytest.fixture
def pc(tmp_path, monkeypatch):
    """A Windows computer with a notes folder, and no Claude yet."""
    monkeypatch.delenv("STUDYSYSTEM_DATA_DIR")  # a student's computer sets none (D-354)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "Local"))
    monkeypatch.setenv("APPDATA", str(tmp_path / "Roaming"))
    monkeypatch.setattr(machine.Path, "home", lambda: tmp_path / "home")
    monkeypatch.setattr(machine.sys, "platform", "win32")
    monkeypatch.setattr(private_view, "active", CHECK)
    monkeypatch.setattr(machine, "study_path", lambda: "/bin/study")
    state = {"on": False, "started": 0}
    monkeypatch.setattr(launcher, "autostart_enabled", lambda: state["on"])
    monkeypatch.setattr(launcher, "_set_autostart", lambda on: state.update(on=on))

    def start_dashboard(port=launcher.PORT):
        state["started"] += 1
        return {"url": launcher.url(port), "started": True}

    monkeypatch.setattr(launcher, "start_dashboard", start_dashboard)
    (tmp_path / "Local").mkdir()
    (tmp_path / "study folder").mkdir()
    state["folder"] = str(tmp_path / "study folder")
    return state


def claude(tmp_path, config=None, place="Local/Claude-Data/claude_x", written=None):
    """A Claude config on that computer."""
    path = tmp_path / place / machine.CONFIG
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(config or {}), encoding="utf-8")
    if written is not None:
        os.utime(path, (written, written))
    return path


def run(*answers):
    return CliRunner().invoke(study, ["setup"], input="".join(f"{a}\n" for a in answers))


def servers(path):
    return json.loads(path.read_text(encoding="utf-8"))["mcpServers"]


# --- the output folder ---------------------------------------------------------------------------


def test_the_saved_folder_wins_over_the_variable(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDYSYSTEM_VAULT_DIR", str(tmp_path / "old"))
    assert settings.output_dir() == tmp_path / "old"  # nothing saved yet

    settings.save_output_dir(tmp_path / "mine")

    assert settings.output_dir() == tmp_path / "mine"


def test_a_settings_file_that_cannot_be_read_never_falls_back(tmp_path, monkeypatch):
    monkeypatch.setenv("STUDYSYSTEM_VAULT_DIR", str(tmp_path / "old"))
    settings.save_output_dir(tmp_path / "mine")
    settings._path().write_text("{half a fi", encoding="utf-8")

    assert settings.output_dir() is None


def test_the_sign_in_answer_does_not_erase_the_folder(tmp_path):
    settings.save_output_dir(tmp_path)
    launcher.keep_answer()

    assert settings.saved_output_dir() == tmp_path


def test_a_pasted_path_loses_its_quotes(tmp_path):
    (tmp_path / "study system").mkdir()

    assert machine.output_folder(f' "{tmp_path / "study system"}" ') == tmp_path / "study system"


@pytest.mark.parametrize("text", ["study", "", "."])
def test_a_path_that_is_not_full_is_refused(text):
    with pytest.raises(StudyError) as caught:
        machine.output_folder(text)
    assert caught.value.code == "output_folder_invalid"


def test_a_folder_that_does_not_exist_is_asked_again_and_not_created(pc, tmp_path):
    typo = tmp_path / "study fodler"

    out = run(typo, pc["folder"], "n")

    assert out.exit_code == 0, out.output
    assert "create the folder" in out.output
    assert not typo.exists()
    assert str(settings.saved_output_dir()) == pc["folder"]


@pytest.mark.parametrize(("answer", "kept"), [("mistake", "study folder"), ("change", "other")])
def test_another_folder_changes_only_on_a_clear_yes(pc, tmp_path, answer, kept):
    (tmp_path / "other").mkdir()
    run(pc["folder"], "n")

    out = run(tmp_path / "other", answer, "n")

    assert out.exit_code == 0, out.output
    assert settings.saved_output_dir() == tmp_path / kept


def test_enter_keeps_the_saved_folder(pc):
    run(pc["folder"], "n")

    out = run("", "n")

    assert out.exit_code == 0, out.output
    assert "mistake" not in out.output
    assert str(settings.saved_output_dir()) == pc["folder"]


def test_a_variable_that_differs_from_the_saved_folder_is_asked_about(pc, tmp_path, monkeypatch):
    run(pc["folder"], "n")
    (tmp_path / "other").mkdir()
    monkeypatch.setenv("STUDYSYSTEM_VAULT_DIR", str(tmp_path / "other"))

    out = run("", "change", "n")

    assert out.exit_code == 0, out.output
    assert settings.saved_output_dir() == tmp_path / "other"


# --- a custom data folder ------------------------------------------------------------------------


def test_a_custom_data_folder_refuses_before_any_question(pc, tmp_path, monkeypatch):
    monkeypatch.setenv("STUDYSYSTEM_DATA_DIR", str(tmp_path / "scratch"))
    path = claude(tmp_path)
    before = path.read_bytes()

    out = run(pc["folder"], "y", "y")

    assert out.exit_code == 1
    assert "setup_custom_data_dir" in out.output
    assert "Full path" not in out.output
    assert path.read_bytes() == before
    assert not settings._path().exists()


def test_an_entry_with_a_custom_data_folder_refuses_the_same_way(pc, tmp_path):
    entry = OLD | {"env": {"STUDYSYSTEM_DATA_DIR": "C:/scratch"}}
    path = claude(tmp_path, {"mcpServers": {"studysystem": entry}})
    before = path.read_bytes()

    out = run(pc["folder"], "y", "y")

    assert out.exit_code == 1
    assert "setup_custom_data_dir" in out.output
    assert path.read_bytes() == before
    assert settings.saved_output_dir() is None


# --- inside Claude, Windows does not see the changes (D-356) -------------------------------------


def redirect(monkeypatch, tmp_path):
    """Every new folder lands in a copy private to Claude, as measured there."""
    real = os.path.realpath

    def redirected(path, **kwargs):
        if "studysystem-check-" in os.path.basename(path):
            return str(tmp_path / "Packages" / "LocalCache" / os.path.basename(path))
        return real(path, **kwargs)

    monkeypatch.setattr(machine.os.path, "realpath", redirected)


def test_inside_claude_setup_refuses_before_any_question(pc, tmp_path, monkeypatch):
    path = claude(tmp_path)
    before = path.read_bytes()
    redirect(monkeypatch, tmp_path)

    out = run(pc["folder"], "y", "y")

    assert out.exit_code == 1
    assert "setup_private_view" in out.output
    assert "Full path" not in out.output
    assert path.read_bytes() == before
    assert not settings._path().exists()
    assert pc["on"] is False


def test_the_check_leaves_no_folder_behind(pc, tmp_path, monkeypatch):
    machine.refuse_private_view()
    redirect(monkeypatch, tmp_path)
    with pytest.raises(StudyError):
        machine.refuse_private_view()

    assert list((tmp_path / "Local").iterdir()) == []


def test_a_check_folder_that_cannot_be_made_does_not_refuse(pc, tmp_path, monkeypatch):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "not there"))

    machine.refuse_private_view()


def test_a_check_folder_that_cannot_be_found_does_not_refuse(pc, tmp_path, monkeypatch):
    def refuse(path, **kwargs):
        raise OSError(5, "access is denied")

    monkeypatch.setattr(machine.os.path, "realpath", refuse)

    machine.refuse_private_view()

    assert list((tmp_path / "Local").iterdir()) == []


@pytest.mark.parametrize("private", [False, True])
def test_a_check_folder_that_cannot_be_removed_keeps_the_answer(pc, tmp_path, monkeypatch, private):
    def refuse(path, **kwargs):
        raise PermissionError(13, "in use")

    if private:
        redirect(monkeypatch, tmp_path)
    with monkeypatch.context() as held:
        held.setattr(machine.os, "rmdir", refuse)
        if private:
            with pytest.raises(StudyError):
                machine.refuse_private_view()
        else:
            machine.refuse_private_view()


def test_a_mac_is_never_tested(pc, tmp_path, monkeypatch):
    monkeypatch.setattr(machine.sys, "platform", "darwin")
    redirect(monkeypatch, tmp_path)

    machine.refuse_private_view()


# --- finding Claude's config ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "place",
    [
        "Local/Claude-Data/claude_pzs8sxrjxfjjc",
        "Local/Packages/Claude_pzs8sxrjxfjjc/LocalCache/Roaming/Claude",
        "Roaming/Claude",
    ],
)
def test_each_windows_place_is_found(pc, tmp_path, place):
    path = claude(tmp_path, place=place)

    assert machine.config_files() == [path]


def test_the_mac_place_is_found(pc, tmp_path, monkeypatch):
    monkeypatch.setattr(machine.sys, "platform", "darwin")
    path = claude(tmp_path, place="home/Library/Application Support/Claude")

    assert machine.config_files() == [path]


def test_a_folder_with_only_the_marker_file_is_no_config(pc, tmp_path):
    (tmp_path / "Roaming" / "Claude").mkdir(parents=True)
    (tmp_path / "Roaming" / "Claude" / ".user-data-left.claude_x").write_text("{}")

    assert machine.config_files() == []


def test_with_several_configs_enter_takes_the_one_written_last(pc, tmp_path):
    stale = claude(tmp_path, place="Roaming/Claude", written=1_000_000)
    used = claude(tmp_path, written=2_000_000)

    out = run("", pc["folder"], "y", "n")

    assert out.exit_code == 0, out.output
    assert servers(used) == {"studysystem": NEW}
    assert "mcpServers" not in json.loads(stale.read_text())


def test_with_no_config_setup_says_how_and_does_the_rest(pc):
    out = run(pc["folder"], "y")

    assert out.exit_code == 0, out.output
    assert "start it once" in out.output
    assert pc["on"] is True
    assert str(settings.saved_output_dir()) == pc["folder"]


# --- writing the entry ---------------------------------------------------------------------------


def test_setup_writes_only_its_entry_and_keeps_a_copy(pc, tmp_path):
    config = {
        "mcpServers": {"other": {"command": "x"}},
        "preferences": {"sidebarMode": "code"},
        "coworkUserFilesPath": "C:/Users/me/Étude",
    }
    path = claude(tmp_path, config)
    before = path.read_bytes()

    out = run(pc["folder"], "y", "n")

    assert out.exit_code == 0, out.output
    assert "tray" in out.output  # how to quit, before the question
    assert json.loads(path.read_text(encoding="utf-8")) == config | {
        "mcpServers": {"other": {"command": "x"}, "studysystem": NEW}
    }
    (copy,) = path.parent.glob("*.bak-*")
    assert copy.read_bytes() == before
    assert sorted(p.name for p in path.parent.iterdir()) == sorted([path.name, copy.name])


def test_a_different_entry_is_shown_and_replaced_on_a_yes(pc, tmp_path):
    entry = OLD | {"env": OLD["env"] | {"STUDYSYSTEM_CHROME": "C:/chrome.exe"}}
    path = claude(tmp_path, {"mcpServers": {"studysystem": entry}})

    out = run(pc["folder"], "y", "y", "n")

    assert out.exit_code == 0, out.output
    assert "studysystem.server" in out.output  # the old one is on screen
    # the old folder goes, every other variable stays (D-349)
    assert servers(path) == {"studysystem": NEW | {"env": {"STUDYSYSTEM_CHROME": "C:/chrome.exe"}}}


@pytest.mark.parametrize("answers", [["n"], ["y", "n"]])  # "replace?", then "is it closed?"
def test_a_no_leaves_the_config_byte_for_byte(pc, tmp_path, answers):
    path = claude(tmp_path, {"mcpServers": {"studysystem": OLD}})
    before = path.read_bytes()

    out = run(pc["folder"], *answers, "n")

    assert out.exit_code == 0, out.output
    assert path.read_bytes() == before
    assert list(path.parent.iterdir()) == [path]


def test_a_second_run_asks_nothing_about_claude(pc, tmp_path):
    path = claude(tmp_path, {"mcpServers": {"studysystem": NEW}})
    before = path.read_bytes()

    out = run(pc["folder"], "n")

    assert out.exit_code == 0, out.output
    assert "already connected" in out.output
    assert path.read_bytes() == before


def test_what_claude_wrote_when_it_quit_is_not_lost(pc, tmp_path):
    """Setup reads the file once to ask, and again to write: Claude quits in between."""
    path = claude(tmp_path, {})
    assert machine.current_entry(path) is None
    claude(tmp_path, {"preferences": {"written": "at quit"}})

    machine.write_entry(path, NEW)

    assert json.loads(path.read_text()) == {
        "preferences": {"written": "at quit"},
        "mcpServers": {"studysystem": NEW},
    }


def test_a_file_with_a_byte_order_mark_is_read(pc, tmp_path):
    path = claude(tmp_path)
    path.write_text('{"preferences": {}}', encoding="utf-8-sig")

    machine.write_entry(path, NEW)

    assert servers(path) == {"studysystem": NEW}


@pytest.mark.parametrize("text", ["{half", "[]", '{"mcpServers": []}', "\xff"])
def test_a_config_that_is_not_claudes_is_untouched_and_the_rest_goes_on(pc, tmp_path, text):
    path = claude(tmp_path)
    path.write_bytes(text.encode("latin-1"))
    before = path.read_bytes()

    out = run(pc["folder"], "n")

    assert out.exit_code == 1
    assert "claude_config_unreadable" in out.output
    assert path.read_bytes() == before
    assert str(settings.saved_output_dir()) == pc["folder"]


def test_a_failed_write_leaves_the_old_file_whole(pc, tmp_path, monkeypatch):
    path = claude(tmp_path, {"preferences": {}})
    before = path.read_bytes()

    def refuse(*_):
        raise PermissionError(13, "in use")

    monkeypatch.setattr(machine.os, "replace", refuse)
    with pytest.raises(StudyError) as caught:
        machine.write_entry(path, NEW)

    assert caught.value.code == "claude_config_not_written"
    assert path.read_bytes() == before
    assert not list(path.parent.glob("*studysystem-new"))


# --- the full path of `study` --------------------------------------------------------------------


@pytest.mark.parametrize(("platform", "name"), [("win32", "study.exe"), ("darwin", "study")])
def test_the_entry_names_the_command_that_runs_setup(tmp_path, monkeypatch, platform, name):
    """Windows hands the path over without `.exe` (measured on a `uv tool install`)."""
    (tmp_path / name).write_text("")
    monkeypatch.setattr(machine.sys, "platform", platform)
    monkeypatch.setattr(machine.sys, "argv", [str(tmp_path / "study"), "setup"])

    assert machine.entry() == {"command": str(tmp_path / name), "args": ["serve"]}


def test_no_installed_command_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(machine.sys, "argv", ["pytest"])
    monkeypatch.setattr(machine.sys, "executable", str(tmp_path / "python"))
    monkeypatch.setattr(machine.shutil, "which", lambda name: None)

    with pytest.raises(StudyError) as caught:
        machine.study_path()

    assert caught.value.code == "study_not_found"


# --- the start at sign-in ------------------------------------------------------------------------


def test_a_yes_turns_the_start_on_and_starts_it_now(pc):
    out = run(pc["folder"], "y")

    assert out.exit_code == 0, out.output
    assert (pc["on"], pc["started"]) == (True, 1)
    assert launcher._asked() is True


@pytest.mark.parametrize("was_on", [False, True])
def test_a_no_leaves_the_start_as_it_is_and_is_not_asked_again(pc, was_on):
    pc["on"] = was_on

    out = run(pc["folder"], "n")

    assert out.exit_code == 0, out.output
    assert (pc["on"], pc["started"]) == (was_on, 0)
    assert "study start" in out.output
    assert ("startup settings" in out.output) is was_on
    assert launcher._asked() is True


def test_where_there_is_no_start_at_sign_in_setup_says_so(pc, monkeypatch):
    monkeypatch.setattr(launcher, "autostart_enabled", lambda: None)

    out = run(pc["folder"])

    assert out.exit_code == 0, out.output
    assert "cannot start at sign-in" in out.output


def test_a_dashboard_that_does_not_start_still_prints_the_entry(pc, monkeypatch):
    def refuse(port=launcher.PORT):
        raise StudyError(code="dashboard_not_started", message="no answer", fix="read the log")

    monkeypatch.setattr(launcher, "start_dashboard", refuse)

    out = run(pc["folder"], "y")

    assert out.exit_code == 1
    assert "dashboard_not_started" in out.output
    assert '"mcpServers"' in out.output
