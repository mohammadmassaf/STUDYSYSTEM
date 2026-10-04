"""a note records where its PDF went, beside its markdown (D-81)

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-04
"""

import sqlalchemy as sa
from alembic import op

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None

# Plain ADD / DROP COLUMN, as in 0003: no table rebuild on either engine. Nullable with no
# default, so no backfill: every existing note starts with its PDF not written - Chapter 6's
# included, which study_export_notes then writes (D-79).


def upgrade() -> None:
    op.add_column("note", sa.Column("pdf_path", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("note", "pdf_path")
