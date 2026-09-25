"""The write unit (D-33): one `BEGIN IMMEDIATE ... COMMIT` around every row a multi-row tool
writes, and a failure anywhere rolls the whole unit back. Which tools are units: "Write units" in
physical-schema.md.

Only public service functions open a unit. Helpers take the unit's `conn` and never open their
own - a second connection inside a unit waits on the lock its own process holds (SQLite), or
commits on its own (Postgres)."""

from collections.abc import Generator
from contextlib import contextmanager

from sqlalchemy import Connection, Engine


@contextmanager
def write_unit(engine: Engine) -> Generator[Connection]:
    """One connection inside one transaction: commits when the body finishes, rolls back and
    re-raises when it fails."""
    with engine.begin() as conn:
        yield conn
