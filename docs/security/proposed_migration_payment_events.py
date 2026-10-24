"""PROPOSED, NOT INSTALLED: add the payment_events table (F-01).

This file lives in docs/security/ on purpose. It is NOT in alembic/versions/, so `alembic upgrade head` does not
see it. It is moved there only after the owner approves it in writing.

Revision ID: e1f7a3c9b5d2
Revises: c7d2e5f8a1b3
Create Date: 2026-10-24 00:00:00.000000

One additive table, nothing else. No existing table or column is touched, and nothing reads or writes it unless
PAYSTACK_STRICT_VERIFY is on.

Idempotent on purpose: `init_db()` calls `Base.metadata.create_all()` at service start, which creates any missing
table, so the table may already exist (empty) by the time this runs. The upgrade then does nothing, and the
downgrade drops it. Running it before or after the first start of the new code gives the same schema.

NOTE for merging: the research branch also adds a migration whose down_revision is c7d2e5f8a1b3 (d4a8f1c3e6b2,
telemetry). Merging both gives two heads; a merge revision is needed then. Do not `alembic stamp head`.
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "e1f7a3c9b5d2"
down_revision: Union[str, Sequence[str], None] = "c7d2e5f8a1b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("payment_events"):
        return
    op.create_table(
        "payment_events",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("event_key", sa.String(160), nullable=False),
        sa.Column("reference", sa.String(100), nullable=True),
        sa.Column("customer_id", sa.String(36), nullable=True),
        sa.Column("amount", sa.Integer, nullable=True),
        sa.Column("currency", sa.String(8), nullable=True),
        sa.Column("outcome", sa.String(24), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("provider", "event_key", name="uq_payment_events_provider_event_key"),
    )


def downgrade() -> None:
    if sa.inspect(op.get_bind()).has_table("payment_events"):
        op.drop_table("payment_events")
