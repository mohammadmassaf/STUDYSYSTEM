"""a course can be dropped - kept as history, gone from the plan and the week (D-217)

Revision ID: 0017
Revises: 0016
Create Date: 2026-10-31
"""

import sqlalchemy as sa
from alembic import op

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None

# A nullable column with no CHECK: a plain ALTER TABLE ADD COLUMN on both engines, so `course`
# is not rebuilt and its expression index `ux_course_code` is never touched. No backfill: no
# course was dropped before this.


def upgrade() -> None:
    op.add_column("course", sa.Column("dropped_at", sa.Text(), nullable=True))


def downgrade() -> None:
    # Dropping a column rebuilds `course` on SQLite, and batch mode cannot see an expression
    # index: `ux_course_code` is dropped first and made again after, as 0016 does.
    op.drop_index("ux_course_code", table_name="course")
    with op.batch_alter_table("course") as batch_op:
        batch_op.drop_column("dropped_at")
    op.create_index(
        "ux_course_code",
        "course",
        [sa.text("user_id"), sa.text("lower(code)"), sa.text("lower(semester_name)")],
        unique=True,
    )
