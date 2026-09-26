"""a course's semester is unique ignoring case, like its code (D-40)

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-26
"""

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None

# An index is dropped and rebuilt on its own - the table is not rebuilt, so batch mode and
# `foreign_keys = ON` never meet here. Written by hand for the reason 0001 gives: autogenerate
# cannot see an expression index.


def upgrade() -> None:
    op.drop_index("ux_course_code", table_name="course")
    op.create_index(
        "ux_course_code",
        "course",
        [sa.text("user_id"), sa.text("lower(code)"), sa.text("lower(semester_name)")],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ux_course_code", table_name="course")
    op.create_index(
        "ux_course_code",
        "course",
        [sa.text("user_id"), sa.text("lower(code)"), sa.text("semester_name")],
        unique=True,
    )
