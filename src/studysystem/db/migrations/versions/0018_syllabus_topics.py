"""a course can declare it has no past papers, and a topic can come from its syllabus (D-218,
D-223, D-224)

Revision ID: 0018
Revises: 0017
Create Date: 2026-11-01
"""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None

OLD = "proposed_by IN ('material', 'profile', 'split')"
NEW = "proposed_by IN ('material', 'profile', 'split', 'syllabus')"
LIVE = "status IN ('proposed', 'active', 'unexamined')"

# Both tables are rebuilt on SQLite, and batch mode cannot see an expression index: `course`'s
# `ux_course_code` and `topic`'s partial `ux_topic_live` are dropped first and made again after,
# as 0016 does. `topic` is a parent table; env.py runs the rebuild with foreign keys off and
# checks them once before COMMIT (D-103). `no_past_papers` backfills to 0: no course had said so.


def _course_code_index() -> None:
    op.create_index(
        "ux_course_code",
        "course",
        [sa.text("user_id"), sa.text("lower(code)"), sa.text("lower(semester_name)")],
        unique=True,
    )


def _topic_live_index() -> None:
    op.create_index(
        "ux_topic_live",
        "topic",
        [sa.text("course_id"), sa.text("lower(name)")],
        unique=True,
        sqlite_where=sa.text(LIVE),
        postgresql_where=sa.text(LIVE),
    )


def _set_sources(check: str) -> None:
    """Replace the `proposed_by` CHECK with `check`, under the same name."""
    op.drop_index("ux_topic_live", table_name="topic")
    with op.batch_alter_table("topic") as batch_op:
        # op.f: the full name as written - the naming convention would prefix it a second time
        batch_op.drop_constraint(op.f("ck_topic_proposed_by_enum"), type_="check")
        batch_op.create_check_constraint(op.f("ck_topic_proposed_by_enum"), check)
    _topic_live_index()


def upgrade() -> None:
    op.drop_index("ux_course_code", table_name="course")
    with op.batch_alter_table("course") as batch_op:
        batch_op.add_column(
            sa.Column(
                "no_past_papers",
                sa.Integer(),
                sa.CheckConstraint(
                    "no_past_papers IN (0,1)", name=op.f("ck_course_no_past_papers_bool")
                ),
                server_default=sa.text("0"),
                nullable=False,
            )
        )
    _course_code_index()
    _set_sources(NEW)


def downgrade() -> None:
    # Refused by the CHECK itself while a syllabus topic exists - v1 deletes no row (D-22).
    _set_sources(OLD)
    op.drop_index("ux_course_code", table_name="course")
    with op.batch_alter_table("course") as batch_op:
        batch_op.drop_constraint(op.f("ck_course_no_past_papers_bool"), type_="check")
        batch_op.drop_column("no_past_papers")
    _course_code_index()
