"""Migrations behind one command (D-22): `study migrate` is the only writer; the server only
checks it is at head and refuses to start otherwise."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from sqlalchemy import Engine

from studysystem.db.engine import READ_ONLY
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
    `study migrate` cannot help - an older build must not touch it."""

    def __init__(self, current, head):
        self.current, self.head = current, head
        super().__init__(
            code="schema_ahead_of_head",
            message=f"database is at {current}, unknown to this build (its head is {head})",
            fix="run the newer build that migrated it, or restore a snapshot taken before that "
            "migrate from the snapshots folder in the data directory",
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


def _known(revision: str) -> bool:
    try:
        return ScriptDirectory.from_config(alembic_config()).get_revision(revision) is not None
    except CommandError:
        return False


def check_schema(engine: Engine) -> None:
    """Raise unless the database is at head: behind (or empty) and ahead are different fixes."""
    current = current_revision(engine)
    head = head_revision()
    if current == head:
        return
    if current is not None and not _known(current):
        raise SchemaAheadOfHead(current, head)
    raise SchemaBehindHead(current, head)


def upgrade(engine: Engine) -> None:
    command.upgrade(alembic_config(engine), "head")


def downgrade(engine: Engine, target: str) -> None:
    command.downgrade(alembic_config(engine), target)
