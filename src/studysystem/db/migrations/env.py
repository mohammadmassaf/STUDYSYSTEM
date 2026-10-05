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
        # SQLite (D-103): batch mode rebuilds a table as copy -> drop -> rename, and with foreign
        # keys enforced the drop of a parent table fails against its children - RESTRICT fires
        # even when deferred. `foreign_keys = OFF` is a no-op inside a transaction, and every
        # pending migration runs inside one, so it is set here, on the raw connection, before
        # anything opens one. Postgres alters in place and needs none of it: `raw` is the sqlite3
        # connection underneath, or None on Postgres.
        sqlite = connection.dialect.name == "sqlite"
        raw = connection.connection.driver_connection if sqlite else None
        if raw is not None:
            raw.execute("PRAGMA foreign_keys = OFF")
        try:
            context.configure(
                connection=connection,
                target_metadata=target_metadata,
                render_as_batch=True,  # SQLite alters a table by rebuilding it (D-08)
            )
            with context.begin_transaction():
                context.run_migrations()
                if raw is not None:
                    # The check the run skipped, once, before COMMIT: a row pointing at a
                    # missing id raises, and the whole run rolls back.
                    dangling = connection.exec_driver_sql("PRAGMA foreign_key_check").fetchall()
                    if dangling:
                        raise RuntimeError(
                            f"migration left rows pointing at missing ids: {dangling[:5]}"
                        )
        finally:
            # The connection goes back to the pool, and every pooled connection enforces them.
            # The run has committed by now, but SQLAlchemy may hold a fresh, empty transaction
            # open - inside it the PRAGMA would be a no-op again - so end it first.
            if raw is not None:
                connection.rollback()
                raw.execute("PRAGMA foreign_keys = ON")


run_migrations()
