"""trades.live_slot: the database keeps limiting an account's live trades, now per slot (B7, live multi-position)

Revision ID: d9b5f7c2e4a6
Revises: c8a4e6b1d3f5
Create Date: 2026-11-18

The index uq_trades_one_live_nonterminal_per_account (audit B1: at most ONE non-terminal LIVE trade per account) would forbid a second slot. This migration replaces it with an equivalent rule per slot:

    trades.live_slot (nullable integer; every live trade carries one, paper trades none) and
    CREATE UNIQUE INDEX uq_trades_live_slot ON trades (account_id, live_slot)
        WHERE status NOT IN ('closed', 'void') AND NOT is_paper AND live_slot IS NOT NULL

Every existing live row is backfilled with slot 0, and with LIVE_MULTI_POSITION off every new live trade takes slot 0, so the effective rule is unchanged: one non-terminal live trade per account.
Additive in effect and reversible. NOT run by Claude; the owner runs it after a backup (restart gate first). The upgrade REFUSES, changing nothing, if an account already has more than one non-terminal
LIVE trade (close or void the extras first), like the migration that created the old index. The downgrade drops the new index and the column and puts the old index back; it REFUSES if an account
holds more than one non-terminal live trade (close the extras first). Idempotent against init_db().
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d9b5f7c2e4a6"
down_revision: Union[str, Sequence[str], None] = "c8a4e6b1d3f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_OLD = "uq_trades_one_live_nonterminal_per_account"
_OLD_WHERE = "status NOT IN ('closed', 'void') AND NOT is_paper"
_NEW = "uq_trades_live_slot"
_NEW_WHERE = "status NOT IN ('closed', 'void') AND NOT is_paper AND live_slot IS NOT NULL"


def _indexes() -> set[str]:
    return {i["name"] for i in sa.inspect(op.get_bind()).get_indexes("trades")}


def _cols() -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns("trades")}


def _refuse_if_several_live() -> None:
    dup = op.get_bind().execute(sa.text(f"SELECT account_id, COUNT(*) FROM trades WHERE {_OLD_WHERE} GROUP BY account_id HAVING COUNT(*) > 1")).fetchall()
    if dup:
        raise RuntimeError(
            f"{len(dup)} account(s) have more than one non-terminal LIVE trade (account ids: {[row[0] for row in dup]}). Close or void the extras by hand first; refusing to guess which trade is the real one."
        )


def upgrade() -> None:
    _refuse_if_several_live()
    if "live_slot" not in _cols():
        op.add_column("trades", sa.Column("live_slot", sa.Integer(), nullable=True))
    op.get_bind().execute(sa.text("UPDATE trades SET live_slot = 0 WHERE NOT is_paper AND live_slot IS NULL"))
    if _OLD in _indexes():
        op.drop_index(_OLD, table_name="trades")
    if _NEW not in _indexes():
        op.create_index(_NEW, "trades", ["account_id", "live_slot"], unique=True, sqlite_where=sa.text(_NEW_WHERE), postgresql_where=sa.text(_NEW_WHERE))


def downgrade() -> None:
    _refuse_if_several_live()
    if _NEW in _indexes():
        op.drop_index(_NEW, table_name="trades")
    if _OLD not in _indexes():
        op.create_index(_OLD, "trades", ["account_id"], unique=True, sqlite_where=sa.text(_OLD_WHERE), postgresql_where=sa.text(_OLD_WHERE))
    if "live_slot" in _cols():
        op.drop_column("trades", "live_slot")
