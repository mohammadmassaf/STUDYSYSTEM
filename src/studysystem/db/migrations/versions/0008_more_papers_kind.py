"""a setup item can ask for more papers because a topic is short of questions (1.18, D-128)

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-10
"""

from alembic import op

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None

KINDS = "'grading', 'credits', 'exam-date', 'target-grade', 'first-material', 'past-papers'"
OLD = f"setup_kind IN ({KINDS})"
NEW = f"setup_kind IN ({KINDS}, 'more-papers')"

# No table points at `plan_item`, so on SQLite this is a plain rebuild of one table, as in 0006.
# Postgres alters the CHECK in place.


def _set_kinds(check: str) -> None:
    """Replace the `setup_kind` CHECK with `check`, under the same name."""
    with op.batch_alter_table("plan_item") as batch_op:
        # op.f: the full name as written - the naming convention would prefix it a second time
        batch_op.drop_constraint(op.f("ck_plan_item_setup_kind_enum"), type_="check")
        batch_op.create_check_constraint(op.f("ck_plan_item_setup_kind_enum"), check)


def upgrade() -> None:
    _set_kinds(NEW)


def downgrade() -> None:
    # Refused by the CHECK itself while a more-papers item exists - v1 deletes no row (D-22).
    _set_kinds(OLD)
