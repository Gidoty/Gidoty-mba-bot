"""trading_accounts.live_slots: how many live trades an account may hold at once (B7, live multi-position)

Revision ID: c8a4e6b1d3f5
Revises: b7f3d5a9c2e4
Create Date: 2026-11-18

Additive, idempotent and reversible. NOT run by Claude; the owner runs it after a backup (restart gate first). One nullable integer on trading_accounts, no backfill (NULL = 1 slot). Read only while
LIVE_MULTI_POSITION is on. The downgrade drops the column (the customers' slot settings are lost; every account is then one slot).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c8a4e6b1d3f5"
down_revision: Union[str, Sequence[str], None] = "b7f3d5a9c2e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _cols() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("trading_accounts")}


def upgrade() -> None:
    if "live_slots" not in _cols():
        op.add_column("trading_accounts", sa.Column("live_slots", sa.Integer(), nullable=True))


def downgrade() -> None:
    if "live_slots" in _cols():
        op.drop_column("trading_accounts", "live_slots")
