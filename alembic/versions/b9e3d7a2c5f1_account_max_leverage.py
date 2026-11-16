"""customer leverage setting: trading_accounts.max_leverage (B6a)

Revision ID: b9e3d7a2c5f1
Revises: a8d2f6c1e3b7
Create Date: 2026-11-16

Additive, idempotent and reversible. NOT run by Claude; the owner runs it after a backup (restart gate first, then stop, back up, migrate, deploy, start).
One nullable integer column on trading_accounts, no backfill: NULL is read as 3 by the code. Nothing reads it for sizing until FUTURES_LEVERAGE_MODE=size_cap
(default strict). Idempotent against init_db(). The downgrade drops the column: every account is then read as 3 again, nothing else changes.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b9e3d7a2c5f1"
down_revision: Union[str, Sequence[str], None] = "a8d2f6c1e3b7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _columns() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("trading_accounts")}


def upgrade() -> None:
    if "max_leverage" not in _columns():
        op.add_column("trading_accounts", sa.Column("max_leverage", sa.Integer(), nullable=True))


def downgrade() -> None:
    if "max_leverage" in _columns():
        op.drop_column("trading_accounts", "max_leverage")
