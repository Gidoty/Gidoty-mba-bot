"""add strategy_pauses (strategy guards)

Revision ID: c9f6a3b1d5e7
Revises: b8e5f2a0c4d6
Create Date: 2026-11-13 00:00:00.000000

One NEW table; no existing table or column is touched. It records a strategy paused for one account by a strategy guard (a loss
streak, or a negative expectancy). Nothing reads or writes it while STRATEGY_GUARDS_MODE is off (the default), and only enforce
mode writes it.

Idempotent: init_db() runs create_all() at every service start, so the table may already exist once this code is deployed.
Downgrade drops the table. Code that predates it ignores it.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c9f6a3b1d5e7"
down_revision: Union[str, Sequence[str], None] = "b8e5f2a0c4d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if not insp.has_table("strategy_pauses"):
        op.create_table(
            "strategy_pauses",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("account_id", sa.String(36), nullable=False),
            sa.Column("strategy_id", sa.String(40), nullable=False),
            sa.Column("code", sa.String(40), nullable=False),
            sa.Column("is_paper", sa.Boolean(), nullable=False),
            sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("resume_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("detail", sa.String(200), nullable=True),
        )
        op.create_index("ix_strategy_pauses_account_strategy", "strategy_pauses", ["account_id", "strategy_id"])


def downgrade() -> None:
    op.drop_index("ix_strategy_pauses_account_strategy", table_name="strategy_pauses")
    op.drop_table("strategy_pauses")
