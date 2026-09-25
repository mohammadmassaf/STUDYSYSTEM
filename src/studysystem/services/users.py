"""Who is asking (D-31). v1 has exactly one user, created by `study migrate`. The front door (a
tool, later the HTTP route) calls `current_user()` and passes `user_id` into services; in v2 only
`current_user()` changes, to read the identity off the request."""

from collections.abc import Sequence

from sqlalchemy import Connection, Engine, insert, select

from studysystem.db.tables import user
from studysystem.errors import StudyError
from studysystem.services.ids import new_id, now
from studysystem.services.units import write_unit


def _user_ids(conn: Connection) -> Sequence[str]:
    query = select(user.c.id)
    return conn.execute(query).scalars().all()


def ensure_user(engine: Engine) -> str:
    """The one v1 user's id, creating the row on the first `study migrate`. Read and insert share
    one unit, so two migrates racing on a new file cannot both find it empty."""
    with write_unit(engine) as conn:
        ids = _user_ids(conn)
        if not ids:
            n_id = new_id()
            crtd_at = now()
            conn.execute(insert(user).values(id=n_id, created_at=crtd_at))
            return n_id
        return ids[0]


def current_user(engine: Engine) -> str:
    """The id every front door passes into services. Read only: creating the user is migrate's
    job, and a read that could write would race the other process (D-31)."""
    with engine.connect() as conn:
        ids = _user_ids(conn)
        if len(ids) == 0:
            raise StudyError(
                code="no_user",
                message="this database has no user yet",
                fix="run: study migrate",
            )
        elif len(ids) > 1:
            raise StudyError(
                code="too_many_users",
                message=f"this database has {len(ids)} users; v1 has exactly one",
                fix="the file was changed outside `study migrate`; restore the latest copy "
                "from the snapshots folder in the data directory",
            )
        return ids[0]
