"""The `study` command: migrate, downgrade, serve. The only process allowed to alter the
database schema (D-22)."""

import datetime
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
    """Plan and track a semester: migrate the database, or serve MCP."""


@study.command("migrate")
def migrate_cmd() -> None:
    """Snapshot the database, then upgrade its schema to head."""
    path = db_path()
    engine, before = _open(path)
    now = datetime.datetime.now().astimezone()
    # One copy per run, not per day: each migrate is a restore point worth keeping.
    target = snapshot(engine, now.date(), f"-premigrate-{now:%H%M%S}")

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
    snap = snapshot(engine, now.date(), f"-predowngrade-{now:%H%M%S}")

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
