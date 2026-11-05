"""a note can retire when its chapter's note is redone (D-250)

Revision ID: 0021
Revises: 0020
Create Date: 2026-11-05
"""

import sqlalchemy as sa
from alembic import op

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None

# No backfill: every existing note is live, so NULL is right for all of them.


def upgrade() -> None:
    with op.batch_alter_table("note") as batch_op:
        batch_op.add_column(sa.Column("retired_at", sa.Text(), nullable=True))


def downgrade() -> None:
    # A retired note comes back as live beside its replacement, which a downgrade accepts: no
    # note text is lost, and the summary task then reads both.
    with op.batch_alter_table("note") as batch_op:
        batch_op.drop_column("retired_at")
