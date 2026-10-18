"""add legal_acceptances table

Revision ID: 1491aa6753c7
Revises: 77dd3e02fe0e
Create Date: 2026-10-18 21:47:52.100129

A brand new, empty table - there is nothing to backfill. An existing
customer simply has zero rows here until they affirmatively accept the
current Terms/Privacy via POST /api/auth/accept-legal (or signs up
fresh, which records both immediately) - see mba_bot.legal and
api/deps.require_legal_acceptance, which is what actually enforces
this. No row here is ever fabricated for pre-existing history.

Autogenerate also picked up an unrelated pre-existing SQLite quirk (an
unnamed unique index on trading_accounts.bot_code vs. the model's
column-level `unique=True`) - left out of this migration since it's
not part of this change and churning it risks losing that column's
existing uniqueness guarantee for no reason.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1491aa6753c7'
down_revision: Union[str, Sequence[str], None] = '77dd3e02fe0e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'legal_acceptances',
        sa.Column('id', sa.String(length=36), nullable=False),
        sa.Column('customer_id', sa.String(length=36), nullable=False),
        sa.Column('document_type', sa.String(length=20), nullable=False),
        sa.Column('version', sa.String(length=20), nullable=False),
        sa.Column('accepted_at', sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint(
            'customer_id', 'document_type', 'version', name='uq_legal_acceptance_customer_document_version'
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('legal_acceptances')
