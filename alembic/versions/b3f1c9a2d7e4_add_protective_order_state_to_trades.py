"""add protective-order and write-ahead state to trades

Revision ID: b3f1c9a2d7e4
Revises: dc1a28b7a42e
Create Date: 2026-10-20 00:00:00.000000

Purely additive. Every new column on `trades` is nullable, so every
existing row (paper or live, open or closed) migrates untouched:

- `protection_status` is NULL for all legacy rows, which the lifecycle
  reads as "no exchange-side stop was ever placed". For a legacy LIVE
  trade that is still open this makes the reconciler place one (or flag
  the trade ATTENTION); for paper and closed rows it means nothing.
- New status values (`pending`, `protecting`, `closing`, `attention`,
  `void`) fit the existing String(10) column; no type change is needed.

It also adds the database-level one-position rule: a PARTIAL UNIQUE INDEX
allowing at most one non-terminal (not closed/void) trade per account.
Creating it on a database that already violates the rule would fail
half-way, so this migration checks first and refuses, loudly, rather
than guessing which duplicate to keep. Code before this revision could
only produce duplicates through a race; none are expected.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b3f1c9a2d7e4"
down_revision: Union[str, Sequence[str], None] = "dc1a28b7a42e"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX = "uq_trades_one_nonterminal_per_account"
_WHERE = "status NOT IN ('closed', 'void')"


def upgrade() -> None:
    bind = op.get_bind()
    duplicates = bind.execute(
        sa.text(
            f"SELECT account_id, COUNT(*) FROM trades WHERE {_WHERE} GROUP BY account_id HAVING COUNT(*) > 1"
        )
    ).fetchall()
    if duplicates:
        raise RuntimeError(
            f"{len(duplicates)} account(s) have more than one non-terminal trade "
            f"(account ids: {[row[0] for row in duplicates]}). Resolve these by hand (close or void the extras) "
            "before upgrading; refusing to guess which trade is the real one."
        )

    op.add_column("trades", sa.Column("pair", sa.String(length=20), nullable=True))
    op.add_column("trades", sa.Column("entry_order_id", sa.String(length=64), nullable=True))
    op.add_column("trades", sa.Column("exit_order_id", sa.String(length=64), nullable=True))
    op.add_column("trades", sa.Column("protective_order_id", sa.String(length=64), nullable=True))
    op.add_column("trades", sa.Column("protective_kind", sa.String(length=24), nullable=True))
    op.add_column("trades", sa.Column("protective_limit_price", sa.Float(), nullable=True))
    op.add_column("trades", sa.Column("protection_status", sa.String(length=12), nullable=True))
    op.add_column("trades", sa.Column("protection_verified_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("trades", sa.Column("attention_reason", sa.String(length=40), nullable=True))
    op.add_column("trades", sa.Column("entry_fee_base", sa.Float(), nullable=True))
    op.add_column("trades", sa.Column("protection_attempt", sa.Integer(), nullable=True, server_default="0"))
    op.add_column("trades", sa.Column("exit_attempt", sa.Integer(), nullable=True, server_default="0"))

    op.create_index(
        _INDEX,
        "trades",
        ["account_id"],
        unique=True,
        sqlite_where=sa.text(_WHERE),
        postgresql_where=sa.text(_WHERE),
    )


def downgrade() -> None:
    op.drop_index(_INDEX, table_name="trades")
    for column in (
        "exit_attempt",
        "protection_attempt",
        "entry_fee_base",
        "attention_reason",
        "protection_verified_at",
        "protection_status",
        "protective_limit_price",
        "protective_kind",
        "protective_order_id",
        "exit_order_id",
        "entry_order_id",
        "pair",
    ):
        op.drop_column("trades", column)
