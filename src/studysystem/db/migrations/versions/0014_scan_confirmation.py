"""a scan's transcription counts once confirmed, and each scanned item names its page (D-210)

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-20
"""

import sqlalchemy as sa
from alembic import op

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None

# `past_exam` gets a plain ADD / DROP COLUMN, as in 0009: a batch rebuild would turn its inline
# CHECKs into table ones, and 0009's own downgrade then cannot drop its column. `practice_item` is
# a batch rebuild, as in 0011 and 0013: SQLite adds a CHECK only by rebuilding the table. env.py
# runs it with foreign keys off and checks them once before COMMIT (D-103).
# Every paper already transcribed was a text-layer one and counted at submit, so it is stamped with
# its task's close time: derive (D-210) reads only stamped papers, and none may drop out.

PAGE_CHECK = "ck_practice_item_page_min"  # full name, passed through op.f() below


def upgrade() -> None:
    op.add_column("past_exam", sa.Column("transcript_confirmed_at", sa.Text(), nullable=True))
    op.execute(
        "UPDATE past_exam SET transcript_confirmed_at = "
        "(SELECT closed_at FROM generation_task WHERE generation_task.id = past_exam.transcript_task_id) "
        "WHERE transcript_task_id IS NOT NULL"
    )
    with op.batch_alter_table("practice_item") as batch_op:
        batch_op.add_column(sa.Column("page", sa.Integer(), nullable=True))
        batch_op.create_check_constraint(op.f(PAGE_CHECK), "page >= 1")


def downgrade() -> None:
    # Dropping the stamp loses no input: a typed paper is stamped again at submit. A scan's page
    # and confirmation are lost, which a downgrade accepts.
    with op.batch_alter_table("practice_item") as batch_op:
        batch_op.drop_constraint(op.f(PAGE_CHECK), type_="check")
        batch_op.drop_column("page")
    op.drop_column("past_exam", "transcript_confirmed_at")
