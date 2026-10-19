"""add capital allocation, start bot, and pair selection mode columns

Revision ID: dc1a28b7a42e
Revises: 1491aa6753c7
Create Date: 2026-10-19 00:00:00.000000

Three additive, nullable (or safely-defaulted) columns on
trading_accounts - no backfill that invents history:

- `pair_selection_mode` gets a server-side default of 'manual' so every
  existing row keeps its exact current single-configured-pair behavior
  with zero behavior change - nothing is auto-selecting anything until a
  customer explicitly switches it.
- `bot_started_at` and `allocated_capital` are left NULL for every
  existing row, on purpose. `bot_started_at` is the fifth, final gate
  `onboarding.resolve_trading_mode` now requires before it will ever
  return LIVE - a NULL here means every pre-existing account, including
  ones that were already trading live under the previous four-gate
  chain, fails closed to PAPER the moment this migration runs, until its
  owner explicitly presses Start Bot. No Start consent is fabricated for
  any existing account, Mainnet or otherwise - see the capital-control
  implementation approval this migration ships.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'dc1a28b7a42e'
down_revision: Union[str, Sequence[str], None] = '1491aa6753c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        "trading_accounts",
        sa.Column("pair_selection_mode", sa.String(length=10), nullable=False, server_default="manual"),
    )
    op.add_column("trading_accounts", sa.Column("bot_started_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("trading_accounts", sa.Column("allocated_capital", sa.Float(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("trading_accounts", "allocated_capital")
    op.drop_column("trading_accounts", "bot_started_at")
    op.drop_column("trading_accounts", "pair_selection_mode")
