"""his lab group, per course - the week shows only that group's meetings (D-227)

Revision ID: 0019
Revises: 0018
Create Date: 2026-11-01
"""

import sqlalchemy as sa
from alembic import op

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None

# A nullable column with no CHECK, as 0017: a plain ALTER TABLE ADD COLUMN on both engines, so
# `course` is not rebuilt and `ux_course_code` is never touched. No backfill: NULL = every group,
# which is what the week showed before this.


def upgrade() -> None:
    op.add_column("course", sa.Column("lab_group", sa.Text(), nullable=True))


def downgrade() -> None:
    # Dropping a column rebuilds `course` on SQLite, and batch mode cannot see an expression
    # index: `ux_course_code` is dropped first and made again after, as 0017 does.
    op.drop_index("ux_course_code", table_name="course")
    with op.batch_alter_table("course") as batch_op:
        batch_op.drop_column("lab_group")
    op.create_index(
        "ux_course_code",
        "course",
        [sa.text("user_id"), sa.text("lower(code)"), sa.text("lower(semester_name)")],
        unique=True,
    )
