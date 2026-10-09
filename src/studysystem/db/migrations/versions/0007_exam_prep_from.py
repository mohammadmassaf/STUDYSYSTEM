"""an exam records the day its prep starts - from then the plan reads its topics by marks (D-119)

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-09
"""

import sqlalchemy as sa
from alembic import op

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None

# Plain ADD / DROP COLUMN, as in 0003 and 0004: no table rebuild on either engine. Nullable with
# no default, so no backfill: every existing exam starts in term mode until he declares its prep.


def upgrade() -> None:
    op.add_column("assessment", sa.Column("prep_from", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("assessment", "prep_from")
