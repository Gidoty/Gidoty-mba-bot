"""add billing_notices (subscription billing, customer reminders)

Revision ID: b8e5f2a0c4d6
Revises: a7d4e1c9b3f5
Create Date: 2026-11-11 00:00:00.000000

One NEW table; no existing table or column is touched. It records that a billing notice (renewal failed, card expiring,
subscription ends soon) was raised for one period, so it goes out once. Nothing reads or writes it while
SUBSCRIPTION_BILLING_ENABLED is off (the default).

Idempotent: init_db() runs create_all() at every service start, so the table may already exist once this code is deployed.
Downgrade drops the table. Code that predates it ignores it.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "b8e5f2a0c4d6"
down_revision: Union[str, Sequence[str], None] = "a7d4e1c9b3f5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if not insp.has_table("billing_notices"):
        op.create_table(
            "billing_notices",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("customer_id", sa.String(36), nullable=False),
            sa.Column("kind", sa.String(24), nullable=False),
            sa.Column("period_key", sa.String(40), nullable=False),
            sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("customer_id", "kind", "period_key", name="uq_billing_notices_customer_kind_period"),
        )
        op.create_index("ix_billing_notices_customer_id", "billing_notices", ["customer_id"])


def downgrade() -> None:
    op.drop_index("ix_billing_notices_customer_id", table_name="billing_notices")
    op.drop_table("billing_notices")
