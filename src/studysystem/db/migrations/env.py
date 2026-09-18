"""Alembic entry point. Runs every migration on a connection from our own engine, so the
PRAGMAs (foreign_keys, WAL, busy_timeout) and BEGIN IMMEDIATE apply here too."""

from alembic import context

from studysystem.db.engine import db_path, make_engine

# 1.4 points this at the Core MetaData so `alembic revision --autogenerate` can diff it.
target_metadata = None


def run_migrations() -> None:
    config = context.config
    # `study migrate` hands us its engine; the bare `alembic` CLI (dev only) gets a fresh one.
    engine = config.attributes.get("engine") or make_engine(db_path())

    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,  # SQLite alters a table by rebuilding it (D-08)
        )
        with context.begin_transaction():
            context.run_migrations()


run_migrations()
