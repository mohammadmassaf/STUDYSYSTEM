"""The write unit (D-33): one `BEGIN IMMEDIATE ... COMMIT` around every row a multi-row tool
writes, and a failure anywhere rolls the whole unit back. Which tools are units: "Write units" in
physical-schema.md.

Only public service functions open a unit. Helpers take the unit's `conn` and never open their
own - a second connection inside a unit waits on the lock its own process holds (SQLite), or
commits on its own (Postgres).

A read that writes nothing opens a `read_unit` instead: same one connection, but no write lock,
so a read never waits behind another process's write unit."""

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import Connection, Engine

from studysystem.db.engine import READ_ONLY


@contextmanager
def write_unit(engine: Engine) -> Generator[Connection]:
    """One connection inside one transaction: commits when the body finishes, rolls back and
    re-raises when it fails."""
    with engine.begin() as conn:
        yield conn


@contextmanager
def read_unit(engine: Engine) -> Generator[Connection]:
    """One connection for reads only: its transaction opens with a plain BEGIN (SQLite), so it
    takes no write lock. Never write through it."""
    with engine.connect() as conn:
        conn.execution_options(**{READ_ONLY: True})
        yield conn
