"""Shared fixtures, and the two-engine rig.

SQLite is the runtime; Postgres runs the same schema in CI so every SQL construct is proved
portable (D-08). Only the schema travels: `engine.py` is SQLite to the bone - WAL,
`BEGIN IMMEDIATE`, `VACUUM INTO` - so the tests that cover it stay SQLite-only, and what runs
on both is the schema, its constraints and the migration that builds it.

Set `STUDYSYSTEM_TEST_PG` to a server URL to turn the Postgres half on:

    STUDYSYSTEM_TEST_PG=postgresql+psycopg://postgres:postgres@localhost:5433/postgres

Unset - a plain laptop with no container running - the Postgres parameters skip, and
`uv run pytest` behaves exactly as it did before.
"""

import os
from collections.abc import Iterator

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.pool import StaticPool

from studysystem.db.tables import metadata

PG_URL = os.environ.get("STUDYSYSTEM_TEST_PG")

# Each consumer gets a database of its own, so none has to care what order tests run in:
# one holds a schema built by `metadata.create_all`, one is wiped and re-migrated, and the
# service tests get a fresh schema per test.
TABLES_DB = "studysystem_tables"
MIGRATE_DB = "studysystem_migrate"
SERVICES_DB = "studysystem_services"

ENGINES = [
    "sqlite",
    pytest.param(
        "postgresql",
        marks=pytest.mark.skipif(PG_URL is None, reason="set STUDYSYSTEM_TEST_PG to run these"),
    ),
]


@pytest.fixture(autouse=True)
def isolated_data_dir(tmp_path, monkeypatch):
    """Every test gets its own data dir; nothing touches the real one."""
    monkeypatch.setenv("STUDYSYSTEM_DATA_DIR", str(tmp_path / "data"))
    return tmp_path / "data"


# --- engines --------------------------------------------------------------


def sqlite_engine() -> Engine:
    """One in-memory database shared by every connection in the test.

    `StaticPool` is what makes it one database: SQLite gives each new connection its own
    empty `:memory:` otherwise. `PRAGMA foreign_keys = ON` is the runtime setting - without
    it SQLite parses foreign keys and then ignores them.
    """
    engine = create_engine("sqlite://", poolclass=StaticPool)

    @event.listens_for(engine, "connect")
    def _fk_on(dbapi_conn, _record):
        dbapi_conn.execute("PRAGMA foreign_keys=ON")

    return engine


def pg_engine(database: str) -> Engine:
    """An engine on `database`, created on the server in `STUDYSYSTEM_TEST_PG` if new."""
    assert PG_URL is not None  # the parameter is skipped without it
    server = make_url(PG_URL)
    admin = create_engine(server, isolation_level="AUTOCOMMIT")
    with admin.connect() as conn:
        found = conn.execute(
            text("SELECT 1 FROM pg_database WHERE datname = :name"), {"name": database}
        ).scalar()
        if not found:
            conn.exec_driver_sql(f'CREATE DATABASE "{database}"')
    admin.dispose()
    return create_engine(server.set(database=database))


@pytest.fixture(scope="session", params=ENGINES)
def schema_engine(request) -> Iterator[Engine]:
    """Both engines, with the whole schema on them - built once, kept for the session."""
    if request.param == "sqlite":
        engine = sqlite_engine()
    else:
        engine = pg_engine(TABLES_DB)
        metadata.drop_all(engine)  # the session before this one left its tables behind
    metadata.create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture(params=ENGINES)
def empty_engine(request) -> Iterator[Engine]:
    """An empty database for the migration to build. Wiped first, so each test sees the
    same nothing a fresh install sees."""
    if request.param == "sqlite":
        from studysystem.db.engine import db_path, make_engine

        engine = make_engine(db_path())  # a file, since `study migrate` snapshots it
    else:
        engine = pg_engine(MIGRATE_DB)
        with engine.begin() as conn:
            conn.exec_driver_sql("DROP SCHEMA public CASCADE")
            conn.exec_driver_sql("CREATE SCHEMA public")
    yield engine
    engine.dispose()


@pytest.fixture(params=ENGINES)
def service_engine(request) -> Iterator[Engine]:
    """The whole schema on an empty database, fresh for every test, for code that writes rows.

    SQLite here is the runtime engine - a file, with `BEGIN IMMEDIATE` on every transaction - not
    the in-memory one above: a write unit proved on a different transaction setup proves nothing.
    """
    if request.param == "sqlite":
        from studysystem.db.engine import db_path, make_engine

        engine = make_engine(db_path())
    else:
        engine = pg_engine(SERVICES_DB)
        metadata.drop_all(engine)
    metadata.create_all(engine)
    yield engine
    engine.dispose()


# --- users ----------------------------------------------------------------


@pytest.fixture
def user_id(service_engine) -> str:
    """The one v1 user, as `study migrate` makes it."""
    from studysystem.services.users import ensure_user

    return ensure_user(service_engine)


@pytest.fixture
def other_user_id(service_engine, user_id) -> str:
    """A second user row - v1 never makes one, but ownership has to hold when it exists."""
    from sqlalchemy import insert

    from studysystem.db.tables import user
    from studysystem.services.ids import new_id, now
    from studysystem.services.units import write_unit

    uid = new_id()
    with write_unit(service_engine) as conn:
        conn.execute(insert(user).values(id=uid, created_at=now()))
    return uid
