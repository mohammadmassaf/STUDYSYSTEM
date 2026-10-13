"""a generation task can be an observation - one paper's format and instructor claims (D-149)

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-13
"""

from alembic import op

revision = "0011"
down_revision = "0010"
branch_labels = None
depends_on = None

OLD = "kind IN ('note', 'practice', 'memory', 'transcription', 'timetable', 'tagging')"
NEW = (
    "kind IN ('note', 'practice', 'memory', 'transcription', 'timetable', 'tagging', 'observation')"
)

# A batch rebuild of a parent table, as in 0005: SQLite changes a CHECK only by rebuilding the
# table. env.py runs it with foreign keys off and checks them once before COMMIT (D-103).


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
    # Refused by the CHECK itself while an observation task exists - v1 deletes no row (D-22).
    _set_kinds(OLD)
