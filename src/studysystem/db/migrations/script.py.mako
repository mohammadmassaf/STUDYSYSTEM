"""${message}

Revision ID: ${up_revision}
Revises: ${down_revision | comma,n}
Create Date: ${create_date}
"""

import sqlalchemy as sa
from alembic import op
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


# Batch mode rebuilds a table by reflecting it, and SQLAlchemy cannot reflect an expression
# index: rebuilding `course`, `topic` or `assessment_slot` silently drops ux_course_code,
# ux_topic_live or ux_assessment_slot_name (the "Skipped unsupported reflection" SAWarning).
# A migration that rebuilds one of them recreates its index after the batch block.


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
