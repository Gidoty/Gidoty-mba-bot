"""pair_skips: why a pair was skipped for an account (B3, small-capital pairs)

Revision ID: f4c7d9b2a6e1
Revises: e3b6c8a1d5f2
Create Date: 2026-11-18

Additive, idempotent and reversible. NOT run by Claude; the owner runs it after a backup (restart gate first). One new table, nothing else changes; nothing writes to it unless
SMALL_CAPITAL_PAIRS is on. The downgrade drops the table (the skip history is lost).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f4c7d9b2a6e1"
down_revision: Union[str, Sequence[str], None] = "e3b6c8a1d5f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if "pair_skips" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "pair_skips",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("account_id", sa.String(36), nullable=False),
        sa.Column("pair", sa.String(20), nullable=False),
        sa.Column("reason", sa.String(40), nullable=False),
        sa.Column("day_key", sa.String(10), nullable=False),
        sa.Column("count", sa.Integer(), nullable=False),
        sa.Column("first_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen", sa.DateTime(timezone=True), nullable=False),
        sa.Column("capital", sa.Float(), nullable=True),
        sa.Column("leverage_cap", sa.Float(), nullable=True),
        sa.Column("min_capital", sa.Float(), nullable=True),
        sa.UniqueConstraint("account_id", "pair", "reason", "day_key", name="uq_pair_skips_account_pair_reason_day"),
    )
    op.create_index("ix_pair_skips_last_seen", "pair_skips", ["last_seen"])


def downgrade() -> None:
    if "pair_skips" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("pair_skips")
