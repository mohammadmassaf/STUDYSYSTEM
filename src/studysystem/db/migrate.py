"""Migrations behind one command (D-22): `study migrate` is the only writer; the server only
checks it is at head and refuses to start otherwise."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import Engine

from studysystem.errors import StudyError

MIGRATIONS_DIR = Path(__file__).parent / "migrations"


class SchemaBehindHead(StudyError):
    def __init__(self, current, head):
        self.current, self.head = current, head
        super().__init__(
            code="schema_behind_head",
            message=f"database is at {current}, head is {head}",
            fix="run: study migrate",
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

    with engine.connect() as conn:
        rev = MigrationContext.configure(conn).get_current_revision()
    return rev


def check_schema(engine: Engine) -> None:
    current = current_revision(engine)
    head = head_revision()
    if current != head:
        raise SchemaBehindHead(current, head)


def upgrade(engine: Engine) -> None:
    command.upgrade(alembic_config(engine), "head")


def downgrade(engine: Engine, target: str) -> None:
    command.downgrade(alembic_config(engine), target)
