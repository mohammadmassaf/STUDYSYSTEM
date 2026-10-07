"""a study session can be practice - past-exam questions on one topic, solved alone (1.16)

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-07
"""

from alembic import op

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None

OLD = "mode IN ('explain-chapter', 'exam-walkthrough', 'mock', 'review-quiz')"
NEW = "mode IN ('explain-chapter', 'exam-walkthrough', 'mock', 'review-quiz', 'practice')"

# `attempt` and `hours_entry` point at `study_session`, so on SQLite this is a parent rebuild
# like 0005 - env.py runs it with foreign keys off (D-103). Postgres alters the CHECK in place.


def _set_modes(check: str) -> None:
    """Replace the `mode` CHECK with `check`, under the same name."""
    with op.batch_alter_table("study_session") as batch_op:
        # op.f: the full name as written - the naming convention would prefix it a second time
        batch_op.drop_constraint(op.f("ck_study_session_mode_enum"), type_="check")
        batch_op.create_check_constraint(op.f("ck_study_session_mode_enum"), check)


def upgrade() -> None:
    _set_modes(NEW)


def downgrade() -> None:
    # Refused by the CHECK itself while a practice session exists - v1 deletes no row (D-22).
    _set_modes(OLD)
