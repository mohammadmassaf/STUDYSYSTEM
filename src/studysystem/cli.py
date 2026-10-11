"""The `study` command: migrate, downgrade, serve. The only process allowed to alter the
database schema (D-22)."""

import datetime

import click
from alembic.util import CommandError

from studysystem.db import migrate
from studysystem.db.engine import db_path, make_engine, snapshot
from studysystem.services.users import ensure_user


@click.group()
def study() -> None:
    """Plan and track a semester: migrate the database, or serve MCP."""


@study.command("migrate")
def migrate_cmd() -> None:
    """Snapshot the database, then upgrade its schema to head."""
    engine = make_engine(db_path())
    now = datetime.datetime.now().astimezone()
    # One copy per run, not per day: each migrate is a restore point worth keeping.
    target = snapshot(engine, now.date(), f"-premigrate-{now:%H%M%S}")

    before = migrate.current_revision(engine)
    try:
        migrate.upgrade(engine)
    except CommandError as e:  # e.g. a database a newer build migrated: a revision unknown here
        raise click.ClickException(f"{e}; nothing was changed") from None
    user_id = ensure_user(engine)  # a no-op after the first run (D-31)

    click.echo(f"snapshot: {target}")
    click.echo(f"{before} -> {migrate.current_revision(engine)}")
    click.echo(f"user: {user_id}")


@study.command()
@click.argument("target")
def downgrade(target: str) -> None:
    """Move the schema back to TARGET (a revision id, or `base` for empty). Development only:
    it drops tables and columns, so it snapshots first, as migrate does (D-23)."""
    engine = make_engine(db_path())
    now = datetime.datetime.now().astimezone()
    snap = snapshot(engine, now.date(), f"-predowngrade-{now:%H%M%S}")

    before = migrate.current_revision(engine)
    try:
        migrate.downgrade(engine, target)
    except CommandError as e:  # an unknown TARGET, most often a typo
        raise click.ClickException(f"{e}; nothing was changed") from None
    click.echo(f"snapshot: {snap}")
    click.echo(f"{before} -> {migrate.current_revision(engine)}")


@study.command()
def serve() -> None:
    """Start the MCP server on stdio."""
    # Imported here: the server pulls in the SDK and every tool module, and a broken import there
    # must not also break `study migrate` - the command that recovers from it.
    from studysystem.server import main

    main()
