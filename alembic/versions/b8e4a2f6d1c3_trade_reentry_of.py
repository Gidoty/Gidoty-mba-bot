"""trade_reentry_of: the trade a re-entry followed

Revision ID: b8e4a2f6d1c3
Revises: a7d3e9f1c5b2
Create Date: 2026-11-24

Additive, idempotent and reversible. NOT run by Claude; the owner runs it after a backup (restart gate first). One nullable column on `trades`, no default, no backfill (NULL = an ordinary entry). create_all() does NOT
add a column to an existing table, so this must run BEFORE the new code starts. The downgrade drops the column.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b8e4a2f6d1c3"
down_revision: Union[str, Sequence[str], None] = "a7d3e9f1c5b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _has_column() -> bool:
    return "reentry_of" in {c["name"] for c in sa.inspect(op.get_bind()).get_columns("trades")}


def upgrade() -> None:
    if not _has_column():
        with op.batch_alter_table("trades") as batch:
            batch.add_column(sa.Column("reentry_of", sa.String(36), nullable=True))


def downgrade() -> None:
    if _has_column():
        with op.batch_alter_table("trades") as batch:
            batch.drop_column("reentry_of")
