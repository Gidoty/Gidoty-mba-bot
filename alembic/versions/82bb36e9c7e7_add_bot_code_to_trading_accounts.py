"""add bot_code to trading_accounts

Revision ID: 82bb36e9c7e7
Revises: 4317105262e4
Create Date: 2026-10-17 00:15:12.563929

"""
import secrets
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '82bb36e9c7e7'
down_revision: Union[str, Sequence[str], None] = '4317105262e4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Deliberately duplicated from mba_bot/botcode.py rather than imported -
# migrations are frozen history and shouldn't depend on app code that will
# keep changing after this file is written. Must stay in sync with that
# module's format by hand if it's ever changed.
_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
_CODE_LENGTH = 6


def _generate_code(existing: set[str]) -> str:
    while True:
        code = "MBA-" + "".join(secrets.choice(_ALPHABET) for _ in range(_CODE_LENGTH))
        if code not in existing:
            existing.add(code)
            return code


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("trading_accounts", sa.Column("bot_code", sa.String(length=20), nullable=True))

    # Backfill any pre-existing rows (nullable at the DB level, but the
    # app always sets this going forward - see models.py) before the
    # unique index goes on, so a real deployment with real customers
    # already in it doesn't end up with accounts missing a support code.
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id FROM trading_accounts WHERE bot_code IS NULL")).fetchall()
    seen: set[str] = set()
    for (account_id,) in rows:
        bind.execute(
            sa.text("UPDATE trading_accounts SET bot_code = :code WHERE id = :id"),
            {"code": _generate_code(seen), "id": account_id},
        )

    op.create_index("ix_trading_accounts_bot_code", "trading_accounts", ["bot_code"], unique=True)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_trading_accounts_bot_code", table_name="trading_accounts")
    op.drop_column("trading_accounts", "bot_code")
