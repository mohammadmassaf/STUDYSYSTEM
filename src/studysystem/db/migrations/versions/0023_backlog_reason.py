"""an off-plan hour can give `backlog` as its reason (D-274)

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-13
"""

from alembic import op

revision = "0023"
down_revision = "0022"
branch_labels = None
depends_on = None

OLD = (
    "deviation_reason IN ('external-deadline', 'disagreed', 'too-tired', 'topic-wrong',"
    " 'unexplained')"
)
NEW = (
    "deviation_reason IN ('external-deadline', 'disagreed', 'too-tired', 'topic-wrong',"
    " 'backlog', 'unexplained')"
)
REASON_CHECK = "ck_hours_entry_deviation_reason_enum"  # the full name, passed through op.f()

# A batch rebuild, as in 0011 and 0020: SQLite changes a CHECK only by rebuilding the table.
# env.py runs it with foreign keys off (D-103). No backfill: no live hour carries a reason yet.


def _set_reasons(check: str) -> None:
    """Replace the `deviation_reason` CHECK with `check`, under the same name."""
    with op.batch_alter_table("hours_entry") as batch_op:
        batch_op.drop_constraint(op.f(REASON_CHECK), type_="check")
        batch_op.create_check_constraint(op.f(REASON_CHECK), check)


def upgrade() -> None:
    _set_reasons(NEW)


def downgrade() -> None:
    # Refused by the CHECK itself while a `backlog` hour exists - v1 deletes no row (D-22).
    _set_reasons(OLD)
