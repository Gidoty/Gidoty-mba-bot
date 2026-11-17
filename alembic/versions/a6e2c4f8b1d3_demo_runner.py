"""demo runner: trading_accounts.demo_enabled_at, trades.is_demo and the table demo_credentials (B4)

Revision ID: a6e2c4f8b1d3
Revises: f4c7d9b2a6e1
Create Date: 2026-11-18

Additive, idempotent and reversible. NOT run by Claude; the owner runs it after a backup (restart gate first). Nothing reads or writes any of it unless DEMO_RUNNER is on and a customer
enters demo keys. Existing trades get is_demo = 0 (server default). The downgrade drops the table and both columns (demo keys and demo flags are lost; demo trades keep existing as ordinary
rows only if the column is dropped, so downgrade only with no demo trade you want to keep apart).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a6e2c4f8b1d3"
down_revision: Union[str, Sequence[str], None] = "f4c7d9b2a6e1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _cols(table: str) -> set[str]:
    return {c["name"] for c in sa.inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if "demo_enabled_at" not in _cols("trading_accounts"):
        op.add_column("trading_accounts", sa.Column("demo_enabled_at", sa.DateTime(timezone=True), nullable=True))
    if "is_demo" not in _cols("trades"):
        op.add_column("trades", sa.Column("is_demo", sa.Boolean(), nullable=False, server_default="0"))
    if "demo_credentials" not in sa.inspect(op.get_bind()).get_table_names():
        op.create_table(
            "demo_credentials",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("account_id", sa.String(36), sa.ForeignKey("trading_accounts.id"), nullable=False, unique=True),
            sa.Column("encrypted_api_key", sa.Text(), nullable=False),
            sa.Column("encrypted_api_secret", sa.Text(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        )


def downgrade() -> None:
    if "demo_credentials" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_table("demo_credentials")
    if "is_demo" in _cols("trades"):
        op.drop_column("trades", "is_demo")
    if "demo_enabled_at" in _cols("trading_accounts"):
        op.drop_column("trading_accounts", "demo_enabled_at")
