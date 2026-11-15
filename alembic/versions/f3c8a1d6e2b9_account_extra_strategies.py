"""per-account opt-in for the new strategies: trading_accounts.extra_strategies (audit H2)

Revision ID: f3c8a1d6e2b9
Revises: e7a2c9d4b1f8
Create Date: 2026-11-15

Additive and reversible. NOT run by Claude; the owner runs it after a backup. NULL means EMA only, exactly as before. The owner sets the column directly in
the database (for example 'rsi_mr_v1,donchian_breakout_v1') for a paper test account; no API or screen writes it. Idempotent against init_db().
The downgrade drops the column: every account is then EMA only (an open rsi_mr_v1 or donchian_breakout_v1 trade keeps its strategy_id only if the earlier
migration is still applied).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f3c8a1d6e2b9"
down_revision: Union[str, Sequence[str], None] = "e7a2c9d4b1f8"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("trading_accounts")}


def upgrade() -> None:
    if "extra_strategies" not in _columns():
        op.add_column("trading_accounts", sa.Column("extra_strategies", sa.String(length=80), nullable=True))


def downgrade() -> None:
    if "extra_strategies" in _columns():
        op.drop_column("trading_accounts", "extra_strategies")
