"""actual commission of the entry and exit fills: trades.entry_fee_quote and trades.exit_fee_quote (PR 2, fee capture)

Revision ID: d2a5b9c7e1f4
Revises: c1f4a8d2e6b3
Create Date: 2026-11-18

Additive, idempotent and reversible. NOT run by Claude; the owner runs it after a backup (restart gate first, then stop, back up, migrate, deploy, start).
Two nullable floats on trades, no backfill (old rows stay NULL = not captured). Nothing in the trading path reads them; they are for reports. Idempotent against init_db().
The downgrade drops both columns: the captured commissions are lost, nothing else changes.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d2a5b9c7e1f4"
down_revision: Union[str, Sequence[str], None] = "c1f4a8d2e6b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_COLUMNS = ("entry_fee_quote", "exit_fee_quote")


def _columns() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("trades")}


def upgrade() -> None:
    existing = _columns()
    for name in _COLUMNS:
        if name not in existing:
            op.add_column("trades", sa.Column(name, sa.Float(), nullable=True))


def downgrade() -> None:
    existing = _columns()
    for name in reversed(_COLUMNS):
        if name in existing:
            op.drop_column("trades", name)
