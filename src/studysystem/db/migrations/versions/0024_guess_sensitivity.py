"""a plan stores how many shown study topics its guesses decide (D-281)

Revision ID: 0024
Revises: 0023
Create Date: 2026-11-14
"""

import sqlalchemy as sa
from alembic import op

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None

# A batch add, as in 0015, so the column's CHECK lands on SQLite. Nullable and no backfill: an
# earlier plan never ran the side ranking, and NULL reads unknown, never 0 (D-281).


def upgrade() -> None:
    with op.batch_alter_table("plan") as batch_op:
        batch_op.add_column(
            sa.Column(
                "guess_changed",
                sa.Integer(),
                sa.CheckConstraint("guess_changed >= 0", name=op.f("ck_plan_guess_changed_min")),
                nullable=True,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("plan") as batch_op:
        batch_op.drop_constraint(op.f("ck_plan_guess_changed_min"), type_="check")
        batch_op.drop_column("guess_changed")
