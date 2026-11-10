"""add subscriptions and billing_payments (subscription billing, stage 0)

Revision ID: f6c3a9d1b8e2
Revises: e5b9c2d7f1a4
Create Date: 2026-11-10 00:00:00.000000

Two NEW tables only; no existing table or column is touched, and nothing is backfilled: a customer with no row is a
legacy lifetime customer. Nothing reads or writes them while SUBSCRIPTION_BILLING_ENABLED is off (the default).

Idempotent: init_db() runs create_all() at every service start, so once this code is deployed the tables may already
exist. Anything already there is left alone.

Downgrade drops both tables. Code that predates them ignores them.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "f6c3a9d1b8e2"
down_revision: Union[str, Sequence[str], None] = "e5b9c2d7f1a4"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("subscriptions"):
        op.create_table(
            "subscriptions",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("customer_id", sa.String(36), sa.ForeignKey("customers.id"), nullable=False),
            sa.Column("status", sa.String(16), nullable=False),
            sa.Column("current_period_start", sa.DateTime(timezone=True), nullable=True),
            sa.Column("current_period_end", sa.DateTime(timezone=True), nullable=True),
            sa.Column("grace_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("provider", sa.String(20), nullable=True),
            sa.Column("provider_subscription_ref", sa.String(100), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("customer_id", name="uq_subscriptions_customer_id"),
        )
    if not insp.has_table("billing_payments"):
        op.create_table(
            "billing_payments",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("customer_id", sa.String(36), nullable=False),
            sa.Column("provider", sa.String(20), nullable=False),
            sa.Column("reference", sa.String(100), nullable=False),
            sa.Column("amount_minor", sa.Integer(), nullable=False),
            sa.Column("currency", sa.String(8), nullable=False),
            sa.Column("usd_value", sa.Numeric(18, 6), nullable=True),
            sa.Column("period_start", sa.DateTime(timezone=True), nullable=True),
            sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
            sa.Column("status", sa.String(12), nullable=False),
            sa.Column("issued_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("paid_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("provider_payment_id", sa.String(100), nullable=True),
            sa.UniqueConstraint("provider", "reference", name="uq_billing_payments_provider_reference"),
        )
        op.create_index("ix_billing_payments_customer_id", "billing_payments", ["customer_id"])


def downgrade() -> None:
    op.drop_index("ix_billing_payments_customer_id", table_name="billing_payments")
    op.drop_table("billing_payments")
    op.drop_table("subscriptions")
