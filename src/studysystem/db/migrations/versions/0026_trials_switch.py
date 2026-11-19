"""a student turns discrimination trials on; off by default (D-312, D-313)

Revision ID: 0026
Revises: 0025
Create Date: 2026-11-19
"""

import sqlalchemy as sa
from alembic import op

revision = "0026"
down_revision = "0025"
branch_labels = None
depends_on = None

# `user` is rebuilt on SQLite (batch mode). No backfill: the server default gives every existing
# user 0, trials off - the D-312 default.


def upgrade() -> None:
    with op.batch_alter_table("user") as batch_op:
        batch_op.add_column(
            sa.Column(
                "trials_on",
                sa.Integer(),
                sa.CheckConstraint("trials_on IN (0,1)", name=op.f("ck_user_trials_on_bool")),
                server_default=sa.text("0"),
                nullable=False,
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("user") as batch_op:
        batch_op.drop_constraint(op.f("ck_user_trials_on_bool"), type_="check")
        batch_op.drop_column("trials_on")
