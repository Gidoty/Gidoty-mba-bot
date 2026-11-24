"""trade_peak_price: the best price reached since entry, on every trade

Revision ID: d4a1b7c3e9f2
Revises: c3f8a2d5b7e9
Create Date: 2026-11-24

Additive, idempotent and reversible. NOT run by Claude; the owner runs it after a backup (restart gate first). One nullable column on `trades`, no default, no backfill: an old open trade has NULL until the first round
that manages it, which then sets it from the entry price, the live price and the candles after the entry (so it is a lower bound for a trade that was already running). A closed trade stays NULL. create_all() does NOT add a column to an
existing table, so the migration must run BEFORE the new code starts (the restart gate). The downgrade drops the column (the peaks recorded so far are lost).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4a1b7c3e9f2"
down_revision: Union[str, Sequence[str], None] = "c3f8a2d5b7e9"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column() -> bool:
    return "peak_price" in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("trades")}


def upgrade() -> None:
    if not _has_column():
        with op.batch_alter_table("trades") as batch:
            batch.add_column(sa.Column("peak_price", sa.Float(), nullable=True))


def downgrade() -> None:
    if _has_column():
        with op.batch_alter_table("trades") as batch:
            batch.drop_column("peak_price")
