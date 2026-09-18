"""The `study` command: migrate, downgrade, serve. The only process allowed to alter the
database schema (D-22)."""

import datetime

import click

from studysystem.db import migrate
from studysystem.db.engine import db_path, make_engine, snapshot
from studysystem.server import main as serve_main


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
    migrate.upgrade(engine)

    click.echo(f"snapshot: {target}")
    click.echo(f"{before} -> {migrate.current_revision(engine)}")


@study.command()
@click.argument("target")
def downgrade(target: str) -> None:
    """Move the schema back to TARGET (a revision id, or `base` for empty)."""
    engine = make_engine(db_path())
    before = migrate.current_revision(engine)
    migrate.downgrade(engine, target)
    click.echo(f"{before} -> {migrate.current_revision(engine)}")


@study.command()
def serve() -> None:
    """Start the MCP server on stdio."""
    serve_main()
