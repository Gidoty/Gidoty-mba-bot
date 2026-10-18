"""add environment and live_trading_enabled_at columns

Revision ID: 77dd3e02fe0e
Revises: 9270f95a8695
Create Date: 2026-10-18 18:53:31.609126

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '77dd3e02fe0e'
down_revision: Union[str, Sequence[str], None] = '9270f95a8695'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # "unknown" - not "testnet", not "mainnet" - for every row that
    # predates this concept. Only a credential explicitly tagged
    # "mainnet" can ever back live trading (see
    # models.TradingAccount.has_mainnet_credential), so an existing
    # credential nobody has re-confirmed stays unable to trade live
    # after this migration runs, full stop, regardless of what
    # has_paid/trial_ends_at/live_bypass_accepted_at already say about
    # it. A static default (not a per-row backfill) is safe here since
    # every existing row gets the exact same, deliberately unprivileged
    # value.
    op.add_column(
        "api_credentials",
        sa.Column("environment", sa.String(length=20), nullable=False, server_default="unknown"),
    )

    # Nullable, no backfill: every existing account simply has never
    # explicitly enabled live trading, which is exactly correct - this
    # migration must not grant that to anyone who didn't already have it
    # (nothing before this migration could set it, so every existing row
    # is correctly NULL already).
    op.add_column("trading_accounts", sa.Column("live_trading_enabled_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("trading_accounts", sa.Column("live_trading_disclosure_text", sa.Text(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("trading_accounts", "live_trading_disclosure_text")
    op.drop_column("trading_accounts", "live_trading_enabled_at")
    op.drop_column("api_credentials", "environment")
