"""trade_stop_moved_at: when a profit rule last moved the protective stop

Revision ID: e5b2c8d4f1a7
Revises: d4a1b7c3e9f2
Create Date: 2026-11-24

Additive, idempotent and reversible. NOT run by Claude; the owner runs it after a backup (restart gate first). One nullable column on `trades`, no default, no backfill (NULL = never moved). create_all() does NOT add
a column to an existing table, so this must run BEFORE the new code starts. The downgrade drops the column.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e5b2c8d4f1a7"
down_revision: Union[str, Sequence[str], None] = "d4a1b7c3e9f2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column() -> bool:
    return "stop_moved_at" in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("trades")}


def upgrade() -> None:
    if not _has_column():
        with op.batch_alter_table("trades") as batch:
            batch.add_column(sa.Column("stop_moved_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    if _has_column():
        with op.batch_alter_table("trades") as batch:
            batch.drop_column("stop_moved_at")
