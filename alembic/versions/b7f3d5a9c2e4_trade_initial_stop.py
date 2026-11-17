"""trades.initial_stop_price: the stop a trade was opened with (B5b, live Donchian trailing stop)

Revision ID: b7f3d5a9c2e4
Revises: a6e2c4f8b1d3
Create Date: 2026-11-18

Additive, idempotent and reversible. NOT run by Claude; the owner runs it after a backup (restart gate first). One nullable float on trades, no backfill (old rows stay NULL and are read as
"the stop resting now"). Written for new live trades; read only by the trailing-stop code, which runs only when LIVE_DONCHIAN is on. The downgrade drops the column.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b7f3d5a9c2e4"
down_revision: Union[str, Sequence[str], None] = "a6e2c4f8b1d3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _cols() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("trades")}


def upgrade() -> None:
    if "initial_stop_price" not in _cols():
        op.add_column("trades", sa.Column("initial_stop_price", sa.Float(), nullable=True))


def downgrade() -> None:
    if "initial_stop_price" in _cols():
        op.drop_column("trades", "initial_stop_price")
