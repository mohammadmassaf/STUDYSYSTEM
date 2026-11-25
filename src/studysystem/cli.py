"""The `study` command: setup, migrate, downgrade, serve, dashboard, start, stop, demo. Only
migrate and downgrade alter an existing schema (D-22); serve and dashboard build a brand-new one
(D-324, D-335)."""

import datetime
import json
from pathlib import Path

import click
from alembic.util import CommandError
from sqlalchemy import Engine
from sqlalchemy.exc import DatabaseError, OperationalError

from studysystem.db import migrate
from studysystem.db.engine import WalUnavailable, db_path, make_engine, snapshot, unavailable
from studysystem.errors import StudyError
from studysystem.services.users import ensure_user


def _line(e: StudyError) -> click.ClickException:
    return click.ClickException(f"{e.code}: {e.message}. {e.fix}")


def _open(path: Path) -> tuple[Engine, str | None]:
    """The engine and the revision it is at. Reading the revision is the first connection, where
    a locked, unreadable or damaged file shows - so it is refused before any copy is taken."""
    engine = make_engine(path)
    try:
        return engine, migrate.current_revision(engine)
    except (DatabaseError, WalUnavailable) as e:
        raise _line(unavailable(path, e)) from None


def _busy(e: OperationalError, path: Path) -> click.ClickException:
    """Another program held the write lock past busy_timeout - with WAL a reader never waits,
    but the run's write does. Any other database error is a real fault and stays a crash (D-36)."""
    if "database is locked" not in str(e.orig):
        raise e
    return _line(
        StudyError(
            code="database_busy",
            message=f"another program is writing to {path}",
            fix="close it (the dashboard, a running server) or let it finish, then run the "
            "command again",
        )
    )


@click.group()
def study() -> None:
    """Plan and track a semester: migrate the database, serve MCP, or serve the dashboard."""


@study.command()
def setup() -> None:
    """Set up this computer: the output folder, Claude, and the dashboard at sign-in. Run it once
    after the install, in a terminal outside Claude (D-332)."""
    from studysystem.services import setup as machine  # imported here for the reason serve gives

    problems = []
    try:
        # Everything that refuses the whole run comes before the first change (D-354, D-356).
        machine.refuse_custom_data_dir()
        machine.refuse_private_view()
        new = machine.entry()
        config = _choose_config(machine.config_files())
        old = None
        if config is not None:
            try:
                old = machine.current_entry(config)
            except StudyError as e:
                if e.code == "setup_custom_data_dir":
                    raise
                problems.append(e)
                config = None
    except StudyError as e:
        raise _line(e) from None

    _ask_output_folder()
    for step in (lambda: _connect_claude(config, old, problems), _ask_sign_in):
        try:
            step()
        except StudyError as e:
            problems.append(e)

    click.echo("\nFor another host (Claude Code, Gemini CLI), paste this entry:")
    click.echo(json.dumps({"mcpServers": {machine.SERVER: new}}, indent=2))
    click.echo("\nFor Codex, run this line instead:")
    click.echo(machine.codex_command())
    if problems:
        raise click.ClickException("; ".join(_line(e).message for e in problems))
    click.echo("\nSetup is done.")


def _choose_config(found: list[Path]) -> Path | None:
    """The Claude config to write: the only one, or the one the student picks (D-344)."""
    if len(found) < 2:
        return found[0] if found else None
    click.echo("This computer has more than one Claude config. The first was used last:")
    for n, path in enumerate(found, 1):
        click.echo(f"  {n}. {path}")
    picked = click.prompt("Which one?", type=click.IntRange(1, len(found)), default=1)
    return found[picked - 1]


def _ask_output_folder() -> None:
    """Ask for the output folder, once; a different one later changes only on a clear yes (D-350,
    D-351)."""
    from studysystem.services import settings
    from studysystem.services import setup as machine

    saved = settings.saved_output_dir()
    variable = settings.variable_output_dir()
    click.echo("The study system writes your notes and summaries in one folder, for all courses.")
    suggested = saved or variable
    while True:
        answer = click.prompt(
            "Full path of that folder", default=str(suggested) if suggested else None
        )
        try:
            folder = machine.output_folder(answer)
            break
        except StudyError as e:
            click.echo(f"{e.message}. {e.fix}.")

    if saved is not None and machine.same_folder(folder, saved) and variable is not None:
        try:
            folder = machine.output_folder(str(variable))
            click.echo(f"{settings.VAULT_ENV} is set on this computer.")
        except StudyError:
            pass  # the variable names no folder: nothing to change to
    if saved is not None and not machine.same_folder(folder, saved):
        click.echo(f"The saved folder is {saved}\nThe other folder is {folder}")
        click.echo(
            "If you change it, files already written stay in the saved folder. Move them yourself "
            "if you want them in the other one."
        )
        picked = click.prompt(
            "Change to the other folder, or was it a mistake?",
            type=click.Choice(["change", "mistake"]),
        )
        if picked == "mistake":
            click.echo(f"Kept: {saved}")
            return
    settings.save_output_dir(folder)
    click.echo(f"Saved: {folder}")


def _connect_claude(config: Path | None, old: dict | None, problems: list[StudyError]) -> None:
    """Put the server entry in Claude's config: the student agrees to replace a different one
    (D-349), and says that Claude is closed (D-345)."""
    from studysystem.services import setup as machine

    if config is None:
        if not problems:  # none found, rather than one that could not be read
            click.echo(
                "\nNo Claude config was found. Install Claude, start it once, quit it, then run "
                "`study setup` again."
            )
        return
    new = machine.wanted_entry(old)
    if old == new:
        click.echo(f"\nClaude is already connected: {config}")
        return
    if old is not None:
        click.echo(f"\nClaude already starts a study system, from {config}:")
        click.echo(json.dumps(old, indent=2))
        click.echo("Setup would replace it with the installed one:")
        click.echo(json.dumps(new, indent=2))
        click.echo(
            "Yes: Claude starts the installed study system from now on. No: Claude keeps the one "
            "it starts today. Your courses, hours and notes stay the same either way, and a copy "
            "of the config is kept."
        )
        if not click.confirm("Replace it?", default=None):
            click.echo("Kept. Claude's config was not changed.")
            return
    else:
        click.echo(f"\nSetup adds the study system to Claude's config: {config}")
    click.echo(f"\n{machine.how_to_quit()}")
    if not click.confirm("Is Claude closed now?", default=None):
        click.echo("Claude's config was not changed. Quit Claude, then run `study setup` again.")
        return
    copy = machine.write_entry(config, new)
    click.echo(f"Claude is connected: {config}\nA copy of the old config: {copy}")


def _ask_sign_in() -> None:
    """Ask about the dashboard's start at sign-in; a no changes nothing (D-289, D-353)."""
    from studysystem.services import launcher

    enabled = launcher.autostart_enabled()
    if enabled is None:
        click.echo(
            "\nThe dashboard cannot start at sign-in on this system. `study start` starts it."
        )
        return
    click.echo()
    if click.confirm(launcher.ASK, default=None):
        launcher.set_autostart(True)
        sent = launcher.start_dashboard()
        click.echo(f"The dashboard starts at every sign-in. It runs now: {sent['url']}")
        click.echo(launcher.invitation())
        return
    launcher.keep_answer()
    if enabled:
        click.echo("It was on already and stays on. Your computer's startup settings turn it off.")
    click.echo("Start the dashboard whenever you want it: `study start`.")


@study.command("migrate")
def migrate_cmd() -> None:
    """Snapshot the database, then upgrade its schema to head."""
    path = db_path()
    engine, before = _open(path)
    now = datetime.datetime.now().astimezone()
    # One copy per run, not per day: each migrate is a restore point worth keeping.
    target = snapshot(engine, now.date(), f"-premigrate-{now:%H%M%S}", unique=True)

    try:
        migrate.upgrade(engine)
    except CommandError as e:  # e.g. a database a newer build migrated: a revision unknown here
        raise click.ClickException(f"{e}; nothing was changed") from None
    except OperationalError as e:
        raise _busy(e, path) from None
    # Printed before the user step: if that one waits out the lock, the landed upgrade and its
    # copy are already on screen.
    click.echo(f"snapshot: {target}")
    click.echo(f"{before} -> {migrate.current_revision(engine)}")

    try:
        user_id = ensure_user(engine)  # a no-op after the first run (D-31)
    except OperationalError as e:
        raise _busy(e, path) from None
    click.echo(f"user: {user_id}")


@study.command()
@click.argument("target")
def downgrade(target: str) -> None:
    """Move the schema back to TARGET (a revision id, or `base` for empty). Development only:
    it drops tables and columns, so it snapshots first, as migrate does (D-23)."""
    path = db_path()
    if not path.exists():  # opening it would leave an empty file behind
        raise click.ClickException(f"no database at {path}; nothing to downgrade")
    engine, before = _open(path)
    now = datetime.datetime.now().astimezone()
    snap = snapshot(engine, now.date(), f"-predowngrade-{now:%H%M%S}", unique=True)

    try:
        migrate.downgrade(engine, target)
    except CommandError as e:  # a TARGET alembic cannot reach, most often a typo
        # The run is one transaction (env.py), so the database is unchanged and the copy only
        # duplicates it: drop it.
        # Asking alembic first would mean re-implementing its target syntax (`head-1`, `0005+1`).
        if snap is not None:
            snap.unlink()
        raise click.ClickException(f"{e}; nothing was changed") from None
    except OperationalError as e:
        refusal = _busy(e, path)  # any other database error re-raises here, keeping the copy
        if snap is not None:
            snap.unlink()  # waited out at BEGIN IMMEDIATE: unchanged, as for a refused target
        raise refusal from None
    click.echo(f"snapshot: {snap}")
    click.echo(f"{before} -> {migrate.current_revision(engine)}")


@study.command()
def serve() -> None:
    """Start the MCP server on stdio."""
    # Imported here: the server pulls in the SDK and every tool module, and a broken import there
    # must not also break `study migrate` - the command that recovers from it.
    from studysystem.server import main

    main()


@study.command()
@click.option("--port", default=8765, show_default=True, help="The port on 127.0.0.1.")
def dashboard(port: int) -> None:
    """Serve the dashboard's HTTP route on 127.0.0.1, beside the MCP server."""
    from studysystem.dashboard import main  # imported here for the reason serve gives

    try:
        main(port)
    except StudyError as e:
        raise _line(e) from None


@study.command()
@click.option("--port", default=8765, show_default=True, help="The port on 127.0.0.1.")
def start(port: int) -> None:
    """Start the dashboard in the background, with no window. After an upgrade (D-342)."""
    from studysystem.services import launcher  # imported here for the reason serve gives

    try:
        sent = launcher.start_dashboard(port)
    except StudyError as e:
        raise _line(e) from None
    click.echo(f"dashboard {'started' if sent['started'] else 'already running'}: {sent['url']}")
    click.echo(launcher.invitation(port))


@study.command()
@click.option("--port", default=8765, show_default=True, help="The port on 127.0.0.1.")
def stop(port: int) -> None:
    """Stop the dashboard that runs in the background. Before an upgrade (D-342)."""
    from studysystem.services import launcher  # imported here for the reason serve gives

    try:
        sent = launcher.stop_dashboard(port)
    except StudyError as e:
        raise _line(e) from None
    click.echo("dashboard stopped" if sent["stopped"] else "no dashboard was running")


@study.command()
@click.option("--port", default=8766, show_default=True, help="The port on 127.0.0.1.")
def demo(port: int) -> None:
    """Build a demo database of invented data in its own folder, then serve the dashboard on it.
    It never opens your own database (D-305)."""
    # Imported here, as `dashboard` does: the seed pulls in every service.
    from studysystem import demo as seeded
    from studysystem.dashboard import main

    folder = seeded.demo_dir()
    click.echo(f"building the demo database in {folder} ...")
    try:
        _, summary, path = seeded.build(folder)
    except StudyError as e:
        raise _line(e) from None
    click.echo(f"database: {path}")
    click.echo(
        f"{summary['courses']} courses, {summary['papers']} past papers, "
        f"{summary['materials']} chapters and slides, {summary['meetings']} class meetings, "
        f"{summary['hours']} hours entries ({summary['minutes']} min), {summary['plans']} plans"
    )
    click.echo(f"dashboard: http://127.0.0.1:{port}  (invented data; Ctrl+C to stop)")
    main(port, locked=False)


# `python -m studysystem.cli dashboard`: how study_dashboard starts it (D-289).
if __name__ == "__main__":
    study()
