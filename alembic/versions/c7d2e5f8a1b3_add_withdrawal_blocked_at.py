"""add withdrawal_blocked_at to trading_accounts

Revision ID: c7d2e5f8a1b3
Revises: b3f1c9a2d7e4
Create Date: 2026-10-21 00:00:00.000000

One additive, nullable column. NULL for every existing account: nobody is
treated as blocked by the migration itself; the block is set only when a
withdrawal-capable key is actually detected at runtime.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c7d2e5f8a1b3"
down_revision: Union[str, Sequence[str], None] = "b3f1c9a2d7e4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("trading_accounts", sa.Column("withdrawal_blocked_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("trading_accounts", "withdrawal_blocked_at")
