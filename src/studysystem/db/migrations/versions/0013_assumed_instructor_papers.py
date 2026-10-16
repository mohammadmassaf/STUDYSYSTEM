"""a profile counts the papers whose instructor it assumed (D-174, D-176)

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-16
"""

import sqlalchemy as sa
from alembic import op

revision = "0013"
down_revision = "0012"
branch_labels = None
depends_on = None

# A batch rebuild of a parent table, as in 0011: SQLite adds a CHECK only by rebuilding the
# table. env.py runs it with foreign keys off and checks them once before COMMIT (D-103).
# Every existing profile reads 0, which is true: before 2.4 derive assumed no instructor (D-150).

CHECK = "ck_exam_profile_count_assumed_min"  # full name, passed through op.f() below


def upgrade() -> None:
    with op.batch_alter_table("exam_profile") as batch_op:
        batch_op.add_column(
            sa.Column(
                "assumed_instructor_papers",
                sa.Integer(),
                server_default=sa.text("0"),
                nullable=False,
            )
        )
        batch_op.create_check_constraint(op.f(CHECK), "assumed_instructor_papers >= 0")


def downgrade() -> None:
    # The count is derived: the next derive writes it again, so dropping it loses no input.
    with op.batch_alter_table("exam_profile") as batch_op:
        batch_op.drop_constraint(op.f(CHECK), type_="check")
        batch_op.drop_column("assumed_instructor_papers")
