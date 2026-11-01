"""a meeting can repeat in odd or even weeks or belong to one lab group, and a course can be
left out of the plan (D-216)

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-31
"""

import sqlalchemy as sa
from alembic import op

revision = "0016"
down_revision = "0015"
branch_labels = None
depends_on = None

# `class_meeting`'s unique key becomes an expression index, because two NULL pattern columns
# would never collide in a plain key. Batch mode rebuilds a table on SQLite and its reflection
# cannot see an expression index, so `ux_course_code` is dropped before `course` is rebuilt and
# made again after (as 0002 made it). Every earlier meeting is a whole-class, every-week row, so
# no backfill is needed; `planned` backfills to 1.


def _course_code_index() -> None:
    op.create_index(
        "ux_course_code",
        "course",
        [sa.text("user_id"), sa.text("lower(code)"), sa.text("lower(semester_name)")],
        unique=True,
    )


def upgrade() -> None:
    with op.batch_alter_table("class_meeting") as batch_op:
        batch_op.drop_constraint(
            op.f("uq_class_meeting_course_id_weekday_start_time_kind"), type_="unique"
        )
        batch_op.add_column(
            sa.Column(
                "week_parity",
                sa.Text(),
                sa.CheckConstraint(
                    "week_parity IN ('odd', 'even')",
                    name=op.f("ck_class_meeting_week_parity_enum"),
                ),
                nullable=True,
            )
        )
        batch_op.add_column(
            sa.Column(
                "group_label",
                sa.Text(),
                sa.CheckConstraint(
                    "group_label IS NULL OR length(group_label) > 0",
                    name=op.f("ck_class_meeting_group_not_empty"),
                ),
                nullable=True,
            )
        )
    op.create_index(
        "ux_class_meeting_slot",
        "class_meeting",
        [
            sa.text("course_id"),
            sa.text("weekday"),
            sa.text("start_time"),
            sa.text("kind"),
            sa.text("coalesce(week_parity, '')"),
            sa.text("coalesce(group_label, '')"),
        ],
        unique=True,
    )
    op.drop_index("ux_course_code", table_name="course")
    with op.batch_alter_table("course") as batch_op:
        batch_op.add_column(
            sa.Column(
                "planned",
                sa.Integer(),
                sa.CheckConstraint("planned IN (0,1)", name=op.f("ck_course_planned_bool")),
                server_default=sa.text("1"),
                nullable=False,
            )
        )
    _course_code_index()


def downgrade() -> None:
    op.drop_index("ux_course_code", table_name="course")
    with op.batch_alter_table("course") as batch_op:
        batch_op.drop_constraint(op.f("ck_course_planned_bool"), type_="check")
        batch_op.drop_column("planned")
    _course_code_index()
    op.drop_index("ux_class_meeting_slot", table_name="class_meeting")
    with op.batch_alter_table("class_meeting") as batch_op:
        batch_op.drop_constraint(op.f("ck_class_meeting_group_not_empty"), type_="check")
        batch_op.drop_column("group_label")
        batch_op.drop_constraint(op.f("ck_class_meeting_week_parity_enum"), type_="check")
        batch_op.drop_column("week_parity")
        batch_op.create_unique_constraint(
            op.f("uq_class_meeting_course_id_weekday_start_time_kind"),
            ["course_id", "weekday", "start_time", "kind"],
        )
