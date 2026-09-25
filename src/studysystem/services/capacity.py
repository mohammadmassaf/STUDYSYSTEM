"""Weekly study capacity, dated and insert-only (D-17). The capacity in force on day D is the row
with the latest `from_date <= D`, ties to the later id - read by the scheduler's shortfall
detector (D-15), not by anything in 1.7. A vacation is two rows: its start, and the return.
"""

from sqlalchemy import Engine, insert  # noqa: F401

from studysystem.db.tables import capacity  # noqa: F401
from studysystem.services import values  # noqa: F401
from studysystem.services.ids import new_id, now  # noqa: F401
from studysystem.services.units import write_unit  # noqa: F401


def set_capacity(engine: Engine, user_id: str, hours_per_week: float | str, from_date: str) -> dict:
    """One new capacity row. Never updates an old one.
    Returns {"capacity_id", "hours_per_week", "from_date"}."""
    # TODO(human):
    #   before the lock: hours above 0 and at most 112 (16 a day, the table's CHECK);
    #     from_date an exact date
    #   insert the row in a unit - one row, but the same way in as every other write
    raise NotImplementedError
