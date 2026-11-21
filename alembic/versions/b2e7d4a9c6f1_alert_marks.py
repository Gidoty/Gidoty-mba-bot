"""alert_marks: when a customer alert was last sent for an account, pair and reason

Revision ID: b2e7d4a9c6f1
Revises: a1d6c3e8b5f7
Create Date: 2026-11-21

Additive, idempotent and reversible. NOT run by Claude; the owner runs it after a backup (restart gate first). One new table, nothing else changes. Until the table exists the code falls back to its
in-memory alert limit (so a start before the migration only means the old behaviour). The downgrade drops the table (the last-alert times are lost; the next alert may be sent once more).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b2e7d4a9c6f1"
down_revision: Union[str, Sequence[str], None] = "a1d6c3e8b5f7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if "alert_marks" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "alert_marks",
        sa.Column("account_id", sa.String(36), primary_key=True),
        sa.Column("pair", sa.String(20), primary_key=True),
        sa.Column("reason", sa.String(48), primary_key=True),
        sa.Column("last_alert_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    if "alert_marks" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("alert_marks")
