"""PROPOSED, NOT INSTALLED: add the payment_references table (F-01 follow-up).

Lives in docs/security/ on purpose: it is NOT in alembic/versions/, so `alembic upgrade head` does not see it.

Revision ID: f2a8b4d0c6e3
Revises: e1f7a3c9b5d2   (the proposed payment_events migration; see docs/security/README.md for the chain)
Create Date: 2026-10-25 00:00:00.000000

One additive table. Idempotent: init_db() runs create_all() at every service start, so the table usually exists already (empty)
when this runs; the upgrade then does nothing. No existing table or column is touched.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "f2a8b4d0c6e3"
down_revision: Union[str, Sequence[str], None] = "e1f7a3c9b5d2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("payment_references"):
        return
    op.create_table(
        "payment_references",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("reference", sa.String(100), nullable=False),
        sa.Column("customer_id", sa.String(36), nullable=False),
        sa.Column("amount", sa.Integer, nullable=False),
        sa.Column("currency", sa.String(8), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider", "reference", name="uq_payment_references_provider_reference"),
    )
    op.create_index("ix_payment_references_customer_id", "payment_references", ["customer_id"])


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("payment_references"):
        op.drop_table("payment_references")
