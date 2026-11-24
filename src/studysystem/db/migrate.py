"""Migrations behind one command (D-22): only `study migrate` changes an existing schema. A
server or dashboard builds a brand-new database (D-324, D-335, D-337), and otherwise only checks
it: behind head it starts and every call refuses (D-325, D-338), ahead of head it does not start."""

import warnings
from collections.abc import Callable
from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine, inspect
from sqlalchemy.exc import SAWarning

from studysystem.db.engine import HOW_TO_RESTORE, READ_ONLY
from studysystem.errors import StudyError

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class SchemaBehindHead(StudyError):
    """An empty database, or one an older build migrated: `study migrate` brings it to head."""

    def __init__(self, current, head):
        self.current, self.head = current, head
        where = "has no schema yet" if current is None else f"is at {current}"
        super().__init__(
            code="schema_behind_head",
            message=f"database {where}, head is {head}",
            fix="run: study migrate",
        )


class SchemaAheadOfHead(StudyError):
    """A database a newer build migrated: this build's migrations do not know its revision, so
    `study migrate` cannot help - an older build must not touch it. `running`: the newer build came
    while this one ran, so a restart runs it, and a restored snapshot would lose data (D-338)."""

    def __init__(self, current, head, running=False):
        self.current, self.head = current, head
        super().__init__(
            code="schema_ahead_of_head",
            message=f"database is at {current}, unknown to this build (its head is {head})",
            fix="restart the app that runs this server, so it runs the newer build; for the "
            "dashboard, sign out of the computer and in again"
            if running
            else "run the newer build that migrated it, or restore a snapshot taken before that "
            f"migrate from the snapshots folder in the data directory: {HOW_TO_RESTORE}",
        )


def alembic_config(engine: Engine | None = None) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", str(MIGRATIONS_DIR))
    if engine is not None:
        cfg.attributes["engine"] = engine

    return cfg


def head_revision() -> str | None:
    cfg = alembic_config()

    return ScriptDirectory.from_config(cfg).get_current_head()


def current_revision(engine: Engine) -> str | None:
    """The revision stamped in the database; a read, so it takes no write lock."""
    with engine.connect() as conn:
        conn.execution_options(**{READ_ONLY: True})
        rev = MigrationContext.configure(conn).get_current_revision()
    return rev


def has_tables(engine: Engine) -> bool:
    """False for a file just created, one a killed first start left behind before its tables were
    saved, or one emptied since. A read, so it takes no write lock."""
    with engine.connect() as conn:
        conn.execution_options(**{READ_ONLY: True})
        return bool(inspect(conn).get_table_names())


def schema_check(engine: Engine, running: bool = True) -> Callable[[], None]:
    """The check a server runs before every call (D-338). Head and the migrations this build knows
    are read here, once: while it runs, an upgrade can replace the files on disk, and its database
    must be compared with the build in memory. Each call of the check reads only the database's
    revision, and raises unless it is head: behind (or empty) and ahead are different fixes."""
    script = ScriptDirectory.from_config(alembic_config())
    head = script.get_current_head()
    known = {migration.revision for migration in script.walk_revisions()}

    def check() -> None:
        current = current_revision(engine)
        if current == head:
            return
        if current is not None and current not in known:
            raise SchemaAheadOfHead(current, head, running)
        raise SchemaBehindHead(current, head)

    return check


def check_schema(engine: Engine) -> None:
    """The same check, once, at the start."""
    schema_check(engine, running=False)()


def upgrade(engine: Engine) -> None:
    # SQLAlchemy cannot reflect an expression index and says so at each table rebuild. The
    # migrations recreate them, and a test lists every index the result must hold, so the lines
    # would only worry a new user reading the host's log. Not silenced in env.py, where
    # autogenerate runs too and a developer needs them (D-27).
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore", "Skipped unsupported reflection of expression-based index", SAWarning
        )
        command.upgrade(alembic_config(engine), "head")


def downgrade(engine: Engine, target: str) -> None:
    command.downgrade(alembic_config(engine), target)
