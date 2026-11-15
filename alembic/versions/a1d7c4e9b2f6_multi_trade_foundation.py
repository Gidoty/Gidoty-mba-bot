"""multi-trade foundation: trades.strategy_id, trading_accounts.pair_list, one open trade per (account, pair)

Revision ID: a1d7c4e9b2f6
Revises: c9f6a3b1d5e7
Create Date: 2026-11-15

Additive and reversible. NOT run by Claude; the owner runs it after a backup (docs/safety/multi_trade.md).

* trades.strategy_id: nullable. NULL means ema_cross_atr_v1. No backfill.
* trading_accounts.pair_list: nullable. NULL means the single `pair`.
* The unique index uq_trades_one_nonterminal_per_account (one non-terminal trade per account) is replaced by
  uq_trades_one_nonterminal_per_pair (one per account AND pair) plus uq_trades_one_nonterminal_no_pair (the old rule, kept for rows
  with no pair, because NULLs are distinct in a unique index). The new rules are looser, so no existing data can violate them.
  The one-position-per-account rule for LIVE accounts moves into code (multi_trade.slot_cap, lifecycle.open_live_trade).

Downgrade restores the old index. It REFUSES, loudly, if any account has more than one non-terminal trade at that moment
(close or void the extras first), exactly like the migration that created the old index.

Idempotent against init_db(): create_all() may already have created the new column or index on a database that was created
fresh with this code, so each step checks first.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1d7c4e9b2f6"
down_revision: Union[str, Sequence[str], None] = "c9f6a3b1d5e7"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD = "uq_trades_one_nonterminal_per_account"
_NEW = "uq_trades_one_nonterminal_per_pair"
_NOPAIR = "uq_trades_one_nonterminal_no_pair"
_WHERE = "status NOT IN ('closed', 'void')"
_WHERE_NOPAIR = "status NOT IN ('closed', 'void') AND pair IS NULL"


def _columns(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def _indexes(table: str) -> set[str]:
    return {i["name"] for i in sa.inspect(op.get_bind()).get_indexes(table)}


def upgrade() -> None:
    if "strategy_id" not in _columns("trades"):
        op.add_column("trades", sa.Column("strategy_id", sa.String(length=40), nullable=True))
    if "pair_list" not in _columns("trading_accounts"):
        op.add_column("trading_accounts", sa.Column("pair_list", sa.String(length=80), nullable=True))
    if _NEW not in _indexes("trades"):
        op.create_index(_NEW, "trades", ["account_id", "pair"], unique=True,
                        sqlite_where=sa.text(_WHERE), postgresql_where=sa.text(_WHERE))
    if _NOPAIR not in _indexes("trades"):
        op.create_index(_NOPAIR, "trades", ["account_id"], unique=True,
                        sqlite_where=sa.text(_WHERE_NOPAIR), postgresql_where=sa.text(_WHERE_NOPAIR))
    if _OLD in _indexes("trades"):
        op.drop_index(_OLD, table_name="trades")


def downgrade() -> None:
    bind = op.get_bind()
    duplicates = bind.execute(
        sa.text(f"SELECT account_id, COUNT(*) FROM trades WHERE {_WHERE} GROUP BY account_id HAVING COUNT(*) > 1")
    ).fetchall()
    if duplicates:
        raise RuntimeError(
            f"{len(duplicates)} account(s) have more than one non-terminal trade (account ids: {[row[0] for row in duplicates]}). "
            "Close or void the extras by hand before downgrading; refusing to guess which trade is the real one."
        )
    if _OLD not in _indexes("trades"):
        op.create_index(_OLD, "trades", ["account_id"], unique=True,
                        sqlite_where=sa.text(_WHERE), postgresql_where=sa.text(_WHERE))
    if _NEW in _indexes("trades"):
        op.drop_index(_NEW, table_name="trades")
    if _NOPAIR in _indexes("trades"):
        op.drop_index(_NOPAIR, table_name="trades")
    if "pair_list" in _columns("trading_accounts"):
        op.drop_column("trading_accounts", "pair_list")
    if "strategy_id" in _columns("trades"):
        op.drop_column("trades", "strategy_id")
