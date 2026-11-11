"""a rescope retires an exam edge and frees a paper's positions (D-261, D-264)

Revision ID: 0022
Revises: 0021
Create Date: 2026-11-10
"""

import sqlalchemy as sa
from alembic import op

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None

# No backfill: no edge has been retired and no item superseded yet, so NULL is right for every
# edge and the narrower index holds every row the old one held.

OLD_WHERE = "past_exam_id IS NOT NULL"
NEW_WHERE = "past_exam_id IS NOT NULL AND state <> 'superseded'"


def _position_index(batch_op, where: str) -> None:
    batch_op.create_index(
        "ux_practice_item_position",
        ["past_exam_id", "position"],
        unique=True,
        sqlite_where=sa.text(where),
        postgresql_where=sa.text(where),
    )


def upgrade() -> None:
    with op.batch_alter_table("coverage") as batch_op:
        batch_op.add_column(sa.Column("retired_at", sa.Text(), nullable=True))
    with op.batch_alter_table("practice_item") as batch_op:
        batch_op.drop_index("ux_practice_item_position")
        _position_index(batch_op, NEW_WHERE)


def downgrade() -> None:
    # A retired edge comes back as live. The old index fails to build once a redo has written a
    # position its superseded item still holds - the downgrade then stops, and nothing changes.
    with op.batch_alter_table("practice_item") as batch_op:
        batch_op.drop_index("ux_practice_item_position")
        _position_index(batch_op, OLD_WHERE)
    with op.batch_alter_table("coverage") as batch_op:
        batch_op.drop_column("retired_at")
