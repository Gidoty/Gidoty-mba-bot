"""add subscription_tokens (subscription billing, stage 1)

Revision ID: a7d4e1c9b3f5
Revises: f6c3a9d1b8e2
Create Date: 2026-11-10 00:00:00.000000

One NEW table; no existing table or column is touched. It holds the provider's subscription token ENCRYPTED. Nothing reads or
writes it while SUBSCRIPTION_BILLING_ENABLED is off (the default).

Idempotent: init_db() runs create_all() at every service start, so the table may already exist once this code is deployed.
Downgrade drops the table. Code that predates it ignores it.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a7d4e1c9b3f5"
down_revision: Union[str, Sequence[str], None] = "f6c3a9d1b8e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if not insp.has_table("subscription_tokens"):
        op.create_table(
            "subscription_tokens",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("subscription_id", sa.String(36), sa.ForeignKey("subscriptions.id"), nullable=False),
            sa.Column("token_enc", sa.Text(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
            sa.UniqueConstraint("subscription_id", name="uq_subscription_tokens_subscription_id"),
        )


def downgrade() -> None:
    op.drop_table("subscription_tokens")
