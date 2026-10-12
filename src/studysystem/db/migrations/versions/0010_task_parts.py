"""a generation task too big for one reply goes out in parts, fetched in order (D-145)

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-12
"""

import sqlalchemy as sa
from alembic import op

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None

# A batch rebuild, as in 0005: `parts_sent <= parts` is a table CHECK across two columns, which
# SQLite adds only by rebuilding the table. env.py runs it with foreign keys off (D-103).
# Backfill: every task so far went out as one reply, so `parts` is 1. A closed task got it
# (`parts_sent` 1); an open one counts as never fetched (0), so its submit waits for a fetch
# under the new rule - the only open task, I3304's tagging, never arrived whole anyway.
# `layout_hash` stays NULL: the content those replies were cut from was never hashed.


def upgrade() -> None:
    with op.batch_alter_table("generation_task") as batch_op:
        batch_op.add_column(
            sa.Column(
                "parts",
                sa.Integer(),
                sa.CheckConstraint("parts >= 1", name=op.f("ck_generation_task_parts_min")),
                server_default=sa.text("1"),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "parts_sent",
                sa.Integer(),
                sa.CheckConstraint(
                    "parts_sent >= 0", name=op.f("ck_generation_task_parts_sent_min")
                ),
                server_default=sa.text("0"),
                nullable=False,
            )
        )
        batch_op.add_column(
            sa.Column(
                "layout_hash",
                sa.Text(),
                sa.CheckConstraint(
                    "length(layout_hash) = 64", name=op.f("ck_generation_task_layout_hash_len")
                ),
                nullable=True,
            )
        )
        batch_op.create_check_constraint(
            op.f("ck_generation_task_parts_sent_max"), "parts_sent <= parts"
        )
    op.execute("UPDATE generation_task SET parts_sent = 1 WHERE status <> 'open'")


def downgrade() -> None:
    # Each CHECK goes by name before its column: batch mode reflects them as table CHECKs, and
    # one left behind names a column the rebuilt table no longer has.
    with op.batch_alter_table("generation_task") as batch_op:
        for name in ("parts_sent_max", "layout_hash_len", "parts_sent_min", "parts_min"):
            batch_op.drop_constraint(op.f(f"ck_generation_task_{name}"), type_="check")
        batch_op.drop_column("layout_hash")
        batch_op.drop_column("parts_sent")
        batch_op.drop_column("parts")
