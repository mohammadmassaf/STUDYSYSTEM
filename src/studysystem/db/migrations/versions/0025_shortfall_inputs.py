"""a course can declare its own hours per credit, and that it is an elective (D-294, D-300)

Revision ID: 0025
Revises: 0024
Create Date: 2026-11-15
"""

import sqlalchemy as sa
from alembic import op

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None

# `course` is rebuilt on SQLite, and batch mode cannot see an expression index: `ux_course_code`
# is dropped first and made again after, as 0018 does. No backfill: every course starts with
# hours_per_credit NULL / `unknown` (the detector reads the default H, D-294) and elective 0.

TIER_RULE = (
    "((hours_per_credit_tier = 'unknown') AND (hours_per_credit is NULL)) OR "
    "(NOT (hours_per_credit_tier = 'unknown') AND NOT(hours_per_credit is NULL))"
)


def _course_code_index() -> None:
    op.create_index(
        "ux_course_code",
        "course",
        [sa.text("user_id"), sa.text("lower(code)"), sa.text("lower(semester_name)")],
        unique=True,
    )


def upgrade() -> None:
    op.drop_index("ux_course_code", table_name="course")
    with op.batch_alter_table("course") as batch_op:
        batch_op.add_column(
            sa.Column(
                "hours_per_credit",
                sa.Float(),
                sa.CheckConstraint(
                    "hours_per_credit > 0", name=op.f("ck_course_hours_per_credit_range")
                ),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "hours_per_credit_tier",
                sa.Text(),
                sa.CheckConstraint(
                    "hours_per_credit_tier IN ('declared', 'inferred', 'unknown')",
                    name=op.f("ck_course_hours_per_credit_tier_enum"),
                ),
                server_default=sa.text("'unknown'"),
                nullable=False,
            )
        )
        batch_op.create_check_constraint(op.f("ck_course_hours_per_credit_tier_rule"), TIER_RULE)
        batch_op.add_column(
            sa.Column(
                "elective",
                sa.Integer(),
                sa.CheckConstraint("elective IN (0,1)", name=op.f("ck_course_elective_bool")),
                server_default=sa.text("0"),
                nullable=False,
            )
        )
    _course_code_index()


def downgrade() -> None:
    op.drop_index("ux_course_code", table_name="course")
    with op.batch_alter_table("course") as batch_op:
        batch_op.drop_constraint(op.f("ck_course_elective_bool"), type_="check")
        batch_op.drop_column("elective")
        batch_op.drop_constraint(op.f("ck_course_hours_per_credit_tier_rule"), type_="check")
        batch_op.drop_constraint(op.f("ck_course_hours_per_credit_tier_enum"), type_="check")
        batch_op.drop_constraint(op.f("ck_course_hours_per_credit_range"), type_="check")
        batch_op.drop_column("hours_per_credit_tier")
        batch_op.drop_column("hours_per_credit")
    _course_code_index()
