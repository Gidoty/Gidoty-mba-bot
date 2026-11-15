"""account-wide daily loss pause: account_day_equity and account_loss_pauses

Revision ID: a8d2f6c1e3b7
Revises: f3c8a1d6e2b9
Create Date: 2026-11-16

Additive, idempotent and reversible. NOT run by Claude; the owner runs it after a backup (restart gate first, then stop, back up, migrate, deploy, start).
Two new tables, no change to an existing one, no backfill. Nothing reads them until ACCOUNT_DAILY_LOSS_PCT is above 0 (default 0 = off).
The downgrade drops both tables: the day-start references and the pause audit rows are lost, and nothing else changes. Idempotent against init_db().
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a8d2f6c1e3b7"
down_revision: Union[str, Sequence[str], None] = "f3c8a1d6e2b9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    existing = _tables()
    if "account_day_equity" not in existing:
        op.create_table(
            "account_day_equity",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("account_id", sa.String(length=36), nullable=False),
            sa.Column("day_key", sa.String(length=10), nullable=False),
            sa.Column("start_equity", sa.Float(), nullable=False),
            sa.Column("approximate", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("captured_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("last_equity", sa.Float(), nullable=True),
            sa.Column("last_equity_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("account_id", "day_key", name="uq_account_day_equity_account_day"),
        )
    if "account_loss_pauses" not in existing:
        op.create_table(
            "account_loss_pauses",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("account_id", sa.String(length=36), nullable=False),
            sa.Column("kind", sa.String(length=20), nullable=False),
            sa.Column("day_key", sa.String(length=10), nullable=False),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("start_equity", sa.Float(), nullable=False),
            sa.Column("equity_at_hit", sa.Float(), nullable=False),
            sa.Column("loss_pct", sa.Float(), nullable=False),
            sa.Column("limit_pct", sa.Float(), nullable=False),
            sa.Column("timezone_name", sa.String(length=40), nullable=False),
            sa.UniqueConstraint("account_id", "kind", "day_key", name="uq_account_loss_pauses_account_kind_day"),
        )


def downgrade() -> None:
    existing = _tables()
    for table in ("account_loss_pauses", "account_day_equity"):
        if table in existing:
            op.drop_table(table)
