"""a generation task can be a tagging - a chapter linked to the topics it teaches (D-93)

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-05
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None

OLD = "kind IN ('note', 'practice', 'memory', 'transcription', 'timetable')"
NEW = "kind IN ('note', 'practice', 'memory', 'transcription', 'timetable', 'tagging')"

# The first migration that rebuilds a parent table: SQLite changes a CHECK only by rebuilding
# the table, and `submission`, `note` and `practice_item` point at `generation_task`. env.py
# runs it with foreign keys off and checks them once before COMMIT (D-103). Postgres alters the
# CHECK in place.


def _set_kinds(check: str) -> None:
    """Replace the `kind` CHECK with `check`, under the same name."""
    with op.batch_alter_table("generation_task") as batch_op:
        # op.f: the full name as written - the metadata's naming convention reaches batch mode
        # and would prefix it a second time
        batch_op.drop_constraint(op.f("ck_generation_task_kind_enum"), type_="check")
        batch_op.create_check_constraint(op.f("ck_generation_task_kind_enum"), check)


def upgrade() -> None:
    _set_kinds(NEW)


def downgrade() -> None:
    # Refused by the CHECK itself while a tagging task exists - v1 deletes no row (D-22).
    _set_kinds(OLD)
