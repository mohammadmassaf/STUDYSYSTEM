"""a course names its folder in the vault, for the note export (D-72)

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-01
"""

import sqlalchemy as sa
from alembic import op

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None

# Plain ADD / DROP COLUMN, not batch mode: both engines alter the column in place (SQLite since
# 3.35), so the table is never rebuilt and batch mode never meets `foreign_keys = ON` (env.py).
# A nullable column with no default needs no backfill: every course starts undeclared.


def upgrade() -> None:
    op.add_column("course", sa.Column("vault_folder", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("course", "vault_folder")
