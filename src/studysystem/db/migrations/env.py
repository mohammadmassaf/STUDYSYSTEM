"""Alembic entry point. Runs every migration on a connection from our own engine, so the
PRAGMAs (foreign_keys, WAL, busy_timeout) and BEGIN IMMEDIATE apply here too."""

from alembic import context

from studysystem.db.engine import db_path, make_engine
from studysystem.db.tables import metadata

# What the code believes the database looks like; `alembic revision --autogenerate` diffs the
# migrations against it.
target_metadata = metadata


def run_migrations() -> None:
    config = context.config
    # `study migrate` hands us its engine; the bare `alembic` CLI (dev only) gets a fresh one.
    engine = config.attributes.get("engine") or make_engine(db_path())

    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # SQLite alters a table by rebuilding it (D-08)
            # 001 only creates tables, so batch mode and `foreign_keys = ON` do not meet.
            # From 002 on they do: batch mode rebuilds a table as copy -> drop -> rename, and
            # with foreign keys enforced the drop fails against children. The fix belongs in
            # the migration that first alters a parent table - turn the PRAGMA off for the
            # batch operation (`PRAGMA legacy_alter_table` / a connection-level toggle), not
            # here, so it is scoped to the one migration that needs it.
        )
        with context.begin_transaction():
            context.run_migrations()


run_migrations()
