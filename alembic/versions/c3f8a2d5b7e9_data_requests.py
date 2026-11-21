"""data_requests: a customer's request to export or delete their data, for the operator to act on

Revision ID: c3f8a2d5b7e9
Revises: b2e7d4a9c6f1
Create Date: 2026-11-21

Additive, idempotent and reversible. NOT run by Claude; the owner runs it after a backup (restart gate first). One new table, nothing else changes. init_db() also creates the table on start if it is missing
(create_all), so a start before the migration works. The downgrade drops the table (the requests recorded so far are lost).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c3f8a2d5b7e9"
down_revision: Union[str, Sequence[str], None] = "b2e7d4a9c6f1"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if "data_requests" in sa.inspect(op.get_bind()).get_table_names():
        return
    op.create_table(
        "data_requests",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("customer_id", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(10), nullable=False),
        sa.Column("status", sa.String(10), nullable=False, server_default="open"),
        sa.Column("requested_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("handled_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_data_requests_customer_id", "data_requests", ["customer_id"])


def downgrade() -> None:
    if "data_requests" in sa.inspect(op.get_bind()).get_table_names():
        op.drop_index("ix_data_requests_customer_id", table_name="data_requests")
        op.drop_table("data_requests")
