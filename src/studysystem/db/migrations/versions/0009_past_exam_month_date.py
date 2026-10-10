"""a past paper that prints only its month is dated the 1st, marked approximate (D-137)

Revision ID: 0009
Revises: 0008
Create Date: 2026-10-10
"""

from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None

# Plain ADD / DROP COLUMN, as in 0003 and 0007: no table rebuild on either engine. Written as SQL
# because the column carries its named CHECK inline, which both engines accept on ADD COLUMN.
# NOT NULL DEFAULT 0 backfills every existing paper as exact - each one printed its day (D-43).


def upgrade() -> None:
    op.execute(
        "ALTER TABLE past_exam ADD COLUMN session_date_approx INTEGER DEFAULT 0 NOT NULL "
        "CONSTRAINT ck_past_exam_session_date_approx_bool CHECK (session_date_approx IN (0,1))"
    )


def downgrade() -> None:
    op.drop_column("past_exam", "session_date_approx")
