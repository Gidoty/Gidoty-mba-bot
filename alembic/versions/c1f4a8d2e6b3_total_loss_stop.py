"""total loss stop: trading_accounts.loss_reference_equity / loss_reference_at and account_total_loss_stops (B1c)

Revision ID: c1f4a8d2e6b3
Revises: b9e3d7a2c5f1
Create Date: 2026-11-17

Additive, idempotent and reversible. NOT run by Claude; the owner runs it after a backup (restart gate first, then stop, back up, migrate, deploy, start).
Two nullable columns on trading_accounts and one new table, no backfill. Nothing reads them until ACCOUNT_TOTAL_LOSS_STOP=1 (default off). Idempotent against init_db().
The downgrade drops the table and the two columns: the reference equities and the stop audit rows are lost, and nothing else changes.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c1f4a8d2e6b3"
down_revision: Union[str, Sequence[str], None] = "b9e3d7a2c5f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("trading_accounts")}


def _tables() -> set[str]:
    return set(sa.inspect(op.get_bind()).get_table_names())


def upgrade() -> None:
    cols = _columns()
    if "loss_reference_equity" not in cols:
        op.add_column("trading_accounts", sa.Column("loss_reference_equity", sa.Float(), nullable=True))
    if "loss_reference_at" not in cols:
        op.add_column("trading_accounts", sa.Column("loss_reference_at", sa.DateTime(timezone=True), nullable=True))
    if "account_total_loss_stops" not in _tables():
        op.create_table(
            "account_total_loss_stops",
            sa.Column("id", sa.String(length=36), primary_key=True),
            sa.Column("account_id", sa.String(length=36), nullable=False),
            sa.Column("triggered_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("reference_equity", sa.Float(), nullable=False),
            sa.Column("equity_at_hit", sa.Float(), nullable=False),
            sa.Column("loss_pct", sa.Float(), nullable=False),
            sa.Column("threshold_pct", sa.Float(), nullable=False),
            sa.Column("risk_pct", sa.Float(), nullable=False),
            sa.Column("reset_at", sa.DateTime(timezone=True), nullable=True),
        )
        op.create_index("ix_account_total_loss_stops_account", "account_total_loss_stops", ["account_id"])


def downgrade() -> None:
    if "account_total_loss_stops" in _tables():
        op.drop_table("account_total_loss_stops")
    cols = _columns()
    for name in ("loss_reference_at", "loss_reference_equity"):
        if name in cols:
            op.drop_column("trading_accounts", name)
