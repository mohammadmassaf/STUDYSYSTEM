"""Weekly study capacity, dated and insert-only (D-17). The capacity in force on day D is the row
with the latest `from_date <= D`, ties to the later id - read by the scheduler's shortfall
detector (D-15), not by anything in 1.7. A vacation is two rows: its start, and the return.
"""

from sqlalchemy import Engine, insert

from studysystem.db.tables import capacity
from studysystem.services import values
from studysystem.services.ids import new_id, now
from studysystem.services.units import write_unit


def set_capacity(engine: Engine, user_id: str, hours_per_week: float | str, from_date: str) -> dict:
    """One new capacity row. Never updates an old one.
    Returns {"capacity_id", "hours_per_week", "from_date"}."""
    hours_per_week = values.number("hours_per_week", hours_per_week, gt=0, le=112)

    date = values.day("from_date", from_date)
    capacity_id = new_id()
    with write_unit(engine) as conn:
        conn.execute(
            insert(capacity).values(
                id=capacity_id,
                user_id=user_id,
                hours_per_week=hours_per_week,
                from_date=date,
                created_at=now(),
            )
        )

    return {"capacity_id": capacity_id, "hours_per_week": hours_per_week, "from_date": date}
