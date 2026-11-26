"""trade_funding_and_fee_asset: Futures funding per trade, and the asset of a commission the bot cannot value (BNB)

Revision ID: a7d3e9f1c5b2
Revises: d4a1b7c3e9f2
Create Date: 2026-11-24

Additive, idempotent and reversible. NOT run by Claude; the owner runs it after a backup (restart gate first). Four nullable columns on `trades` (funding_quote, funding_read_at, fee_asset, fee_asset_amount), no
defaults, no backfill: a closed trade stays NULL (funding unknown) and reports say so. create_all() does NOT add a column to an existing table, so this must run BEFORE the new code starts. The downgrade drops
the four columns (the funding and fee assets recorded so far are lost).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a7d3e9f1c5b2"
down_revision: Union[str, Sequence[str], None] = "d4a1b7c3e9f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COLUMNS = (
    ("funding_quote", sa.Float()),
    ("funding_read_at", sa.DateTime(timezone=True)),
    ("fee_asset", sa.String(12)),
    ("fee_asset_amount", sa.Float()),
)


def _have() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("trades")}


def upgrade() -> None:
    have = _have()
    with op.batch_alter_table("trades") as batch:
        for name, type_ in COLUMNS:
            if name not in have:
                batch.add_column(sa.Column(name, type_, nullable=True))


def downgrade() -> None:
    have = _have()
    with op.batch_alter_table("trades") as batch:
        for name, _ in COLUMNS:
            if name in have:
                batch.drop_column(name)
