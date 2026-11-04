"""minutes can go on a chapter, and a study item stores the chapter it shows under (D-238, D-240)

Revision ID: 0020
Revises: 0019
Create Date: 2026-11-04
"""

import sqlalchemy as sa
from alembic import op

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None

OLD_SCOPE = "NOT (topic_id IS NOT NULL AND setup_key IS NOT NULL)"
NEW_SCOPE = (
    "CASE WHEN topic_id IS NULL THEN 0 ELSE 1 END"
    " + CASE WHEN setup_key IS NULL THEN 0 ELSE 1 END"
    " + CASE WHEN material_id IS NULL THEN 0 ELSE 1 END <= 1"
)
SCOPE_CHECK = "ck_hours_entry_one_scope"  # full names, passed through op.f() below
CHAPTER_CHECK = "ck_plan_item_chapter_study_only"
HOURS_FK = "fk_hours_entry_material_id"
ITEM_FK = "fk_plan_item_material_id"

# Both tables are batch rebuilds, as in 0008 and 0014: SQLite adds a CHECK or a foreign key only
# by rebuilding the table. env.py runs them with foreign keys off (D-103). No backfill: existing
# hours keep their scopes, and the study items already stored stay NULL until a new plan (D-240).


def upgrade() -> None:
    with op.batch_alter_table("hours_entry") as batch_op:
        batch_op.add_column(sa.Column("material_id", sa.Text(), nullable=True))
        batch_op.create_foreign_key(
            op.f(HOURS_FK), "material", ["material_id"], ["id"], ondelete="RESTRICT"
        )
        batch_op.drop_constraint(op.f(SCOPE_CHECK), type_="check")
        batch_op.create_check_constraint(op.f(SCOPE_CHECK), NEW_SCOPE)
    with op.batch_alter_table("plan_item") as batch_op:
        batch_op.add_column(sa.Column("material_id", sa.Text(), nullable=True))
        batch_op.create_foreign_key(
            op.f(ITEM_FK), "material", ["material_id"], ["id"], ondelete="RESTRICT"
        )
        batch_op.create_check_constraint(
            op.f(CHAPTER_CHECK), "NOT (material_id IS NOT NULL) OR (lane = 'study')"
        )


def downgrade() -> None:
    # A chapter's minutes lose their chapter and stay as course-level hours, which a downgrade
    # accepts: the old CHECK allows that, and no minute is lost.
    with op.batch_alter_table("plan_item") as batch_op:
        batch_op.drop_constraint(op.f(CHAPTER_CHECK), type_="check")
        batch_op.drop_constraint(op.f(ITEM_FK), type_="foreignkey")
        batch_op.drop_column("material_id")
    with op.batch_alter_table("hours_entry") as batch_op:
        batch_op.drop_constraint(op.f(SCOPE_CHECK), type_="check")
        batch_op.drop_constraint(op.f(HOURS_FK), type_="foreignkey")
        batch_op.drop_column("material_id")
        batch_op.create_check_constraint(op.f(SCOPE_CHECK), OLD_SCOPE)
