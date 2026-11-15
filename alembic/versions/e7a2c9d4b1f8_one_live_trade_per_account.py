"""one non-terminal LIVE trade per account again (audit finding B1)

Revision ID: e7a2c9d4b1f8
Revises: a1d7c4e9b2f6
Create Date: 2026-11-15

The multi-trade foundation (a1d7c4e9b2f6) replaced the unique index "one non-terminal trade per account" with "one per (account, pair)" for every
account. That moved the live rule out of the database and into a SELECT followed by an INSERT, which two scheduler processes can interleave
(audit B1: two live trades on different pairs, reproduced). This migration puts the database rule back for LIVE rows only:

    CREATE UNIQUE INDEX uq_trades_one_live_nonterminal_per_account ON trades (account_id)
        WHERE status NOT IN ('closed', 'void') AND NOT is_paper

Paper multi-trade is not affected (paper rows are outside the index). Additive and reversible. NOT run by Claude; the owner runs it after a backup.

The upgrade REFUSES, loudly and changing nothing, if an account already has more than one non-terminal LIVE trade (close or void the extras by hand
first), exactly like the migrations that created the earlier indexes. The downgrade only drops the index. Idempotent against init_db().
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e7a2c9d4b1f8"
down_revision: Union[str, Sequence[str], None] = "a1d7c4e9b2f6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_INDEX = "uq_trades_one_live_nonterminal_per_account"
_WHERE = "status NOT IN ('closed', 'void') AND NOT is_paper"


def _indexes() -> set[str]:
    return {i["name"] for i in sa.inspect(op.get_bind()).get_indexes("trades")}


def upgrade() -> None:
    if _INDEX in _indexes():
        return
    duplicates = op.get_bind().execute(
        sa.text(f"SELECT account_id, COUNT(*) FROM trades WHERE {_WHERE} GROUP BY account_id HAVING COUNT(*) > 1")
    ).fetchall()
    if duplicates:
        raise RuntimeError(
            f"{len(duplicates)} account(s) have more than one non-terminal LIVE trade (account ids: {[row[0] for row in duplicates]}). "
            "Close or void the extras by hand before upgrading; refusing to guess which trade is the real one."
        )
    op.create_index(_INDEX, "trades", ["account_id"], unique=True, sqlite_where=sa.text(_WHERE), postgresql_where=sa.text(_WHERE))


def downgrade() -> None:
    if _INDEX in _indexes():
        op.drop_index(_INDEX, table_name="trades")
