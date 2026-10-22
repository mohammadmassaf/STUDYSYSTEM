"""a practice task remembers how many items the student asked for (D-211)

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-22
"""

import sqlalchemy as sa
from alembic import op

revision = "0015"
down_revision = "0014"
branch_labels = None
depends_on = None

# A batch add, as in 0010, so the column's CHECK lands on SQLite. Nullable and no backfill: every
# earlier task is a kind that asks for no count, and NULL means the host chooses.


def upgrade() -> None:
    with op.batch_alter_table("generation_task") as batch_op:
        batch_op.add_column(
            sa.Column(
                "item_count",
                sa.Integer(),
                sa.CheckConstraint(
                    "item_count >= 1", name=op.f("ck_generation_task_item_count_min")
                ),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("generation_task") as batch_op:
        batch_op.drop_constraint(op.f("ck_generation_task_item_count_min"), type_="check")
        batch_op.drop_column("item_count")
