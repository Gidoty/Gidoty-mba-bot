"""customer daily loss limit in dollars: trading_accounts.daily_loss_limit_* and account_loss_pauses.limit_usd

Revision ID: e3b6c8a1d5f2
Revises: d2a5b9c7e1f4
Create Date: 2026-11-18

Additive, idempotent and reversible. NOT run by Claude; the owner runs it after a backup (restart gate first, then stop, back up, migrate, deploy, start).
Four columns on trading_accounts (amount, pending amount, pending start day, default flag) and one nullable float on account_loss_pauses. No data is written here: the one-time
default for existing live accounts is a startup step (daily_limit.ensure_defaults), logged once. Idempotent against init_db(). The downgrade drops the columns: the amounts are lost.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e3b6c8a1d5f2"
down_revision: Union[str, Sequence[str], None] = "d2a5b9c7e1f4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_ACCOUNT_COLUMNS = (
    ("daily_loss_limit_usd", lambda: sa.Column("daily_loss_limit_usd", sa.Float(), nullable=True)),
    ("daily_loss_limit_pending_usd", lambda: sa.Column("daily_loss_limit_pending_usd", sa.Float(), nullable=True)),
    ("daily_loss_limit_pending_from", lambda: sa.Column("daily_loss_limit_pending_from", sa.String(10), nullable=True)),
    ("daily_loss_limit_is_default", lambda: sa.Column("daily_loss_limit_is_default", sa.Boolean(), nullable=False, server_default="0")),
)


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    existing = _columns("trading_accounts")
    for name, make in _ACCOUNT_COLUMNS:
        if name not in existing:
            op.add_column("trading_accounts", make())
    if "limit_usd" not in _columns("account_loss_pauses"):
        op.add_column("account_loss_pauses", sa.Column("limit_usd", sa.Float(), nullable=True))


def downgrade() -> None:
    if "limit_usd" in _columns("account_loss_pauses"):
        op.drop_column("account_loss_pauses", "limit_usd")
    existing = _columns("trading_accounts")
    for name, _ in reversed(_ACCOUNT_COLUMNS):
        if name in existing:
            op.drop_column("trading_accounts", name)
