"""a profile version records the observations it kept, one `profile_claim` row each (D-156)

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-14
"""

import sqlalchemy as sa
from alembic import op

revision = "0012"
down_revision = "0011"
branch_labels = None
depends_on = None

# A new table only - no rebuild, so no batch mode. No owner column, as `topic_weight`: its owner
# is reached through both FKs (physical-schema.md, "Ownership").


def upgrade() -> None:
    op.create_table(
        "profile_claim",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("exam_profile_id", sa.Text(), nullable=False),
        sa.Column("observation_id", sa.Text(), nullable=False),
        sa.Column("created_at", sa.Text(), nullable=False),
        sa.CheckConstraint("length(id) = 26", name=op.f("ck_profile_claim_id_ulid")),
        sa.ForeignKeyConstraint(
            ["exam_profile_id"],
            ["exam_profile.id"],
            name=op.f("fk_profile_claim_exam_profile_id"),
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["observation_id"],
            ["observation.id"],
            name=op.f("fk_profile_claim_observation_id"),
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_profile_claim")),
        sa.UniqueConstraint(
            "exam_profile_id",
            "observation_id",
            name=op.f("uq_profile_claim_exam_profile_id_observation_id"),
        ),
    )


def downgrade() -> None:
    # The rows are derived (D-156): the next derive writes them again, so dropping them loses
    # no input.
    op.drop_table("profile_claim")
