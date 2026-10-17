"""add runtime status columns to trading_accounts

Revision ID: 9270f95a8695
Revises: 82bb36e9c7e7
Create Date: 2026-10-17 17:32:53.172990

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9270f95a8695'
down_revision: Union[str, Sequence[str], None] = '82bb36e9c7e7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # All nullable, no backfill needed - every existing row simply has no
    # recorded runtime state yet, which is exactly correct until the
    # scheduler's next cycle for that account fills them in.
    op.add_column("trading_accounts", sa.Column("last_scanned_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("trading_accounts", sa.Column("last_signal", sa.String(length=30), nullable=True))
    op.add_column("trading_accounts", sa.Column("last_signal_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("trading_accounts", sa.Column("last_error", sa.String(length=500), nullable=True))
    op.add_column("trading_accounts", sa.Column("last_error_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("trading_accounts", sa.Column("live_notified_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("trading_accounts", "live_notified_at")
    op.drop_column("trading_accounts", "last_error_at")
    op.drop_column("trading_accounts", "last_error")
    op.drop_column("trading_accounts", "last_signal_at")
    op.drop_column("trading_accounts", "last_signal")
    op.drop_column("trading_accounts", "last_scanned_at")
