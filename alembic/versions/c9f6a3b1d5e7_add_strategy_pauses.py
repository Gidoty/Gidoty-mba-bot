"""add strategy_pauses (strategy guards)

Revision ID: c9f6a3b1d5e7
Revises: b8e5f2a0c4d6
Create Date: 2026-11-13 00:00:00.000000

One NEW table; no existing table or column is touched. It records a strategy paused for one account by a strategy guard (a loss
streak, or a negative expectancy). Nothing reads or writes it while STRATEGY_GUARDS_MODE is off (the default), and only enforce
mode writes it.

Idempotent: init_db() runs create_all() at every service start, so the table may already exist once this code is deployed.
Downgrade drops the table. Code that predates it ignores it.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "c9f6a3b1d5e7"
down_revision: Union[str, Sequence[str], None] = "b8e5f2a0c4d6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if not insp.has_table("strategy_pauses"):
        op.create_table(
            "strategy_pauses",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("account_id", sa.String(36), nullable=False),
            sa.Column("strategy_id", sa.String(40), nullable=False),
            sa.Column("code", sa.String(40), nullable=False),
            sa.Column("is_paper", sa.Boolean(), nullable=False),
            sa.Column("paused_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("resume_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("detail", sa.String(200), nullable=True),
        )
        op.create_index("ix_strategy_pauses_account_strategy", "strategy_pauses", ["account_id", "strategy_id"])


def _active_pauses() -> int:
    """Pauses with no release and no end, or an end still in the future. An expectancy pause has no end: it lasts until the owner releases it."""
    from datetime import datetime, timezone

    rows = op.get_bind().execute(sa.text("SELECT resume_at FROM strategy_pauses WHERE released_at IS NULL")).fetchall()
    now = datetime.now(timezone.utc)
    active = 0
    for (resume_at,) in rows:
        if resume_at is None:
            active += 1
            continue
        if isinstance(resume_at, str):
            resume_at = datetime.fromisoformat(resume_at.replace(" ", "T"))
        if resume_at.tzinfo is None:
            resume_at = resume_at.replace(tzinfo=timezone.utc)
        if resume_at > now:
            active += 1
    return active


def downgrade() -> None:
    # Audit M6: dropping the table silently ENDS every pause, including an expectancy pause that is documented to end only when the owner releases it.
    # Refuse (changing nothing) while one is active; release it with `python -m mba_bot.strategy_pause release` first, if that is really what is wanted.
    if sa.inspect(op.get_bind()).has_table("strategy_pauses"):
        n = _active_pauses()
        if n:
            raise RuntimeError(
                f"{n} active strategy pause(s) exist. Downgrading would drop the table and silently end them. Release them first "
                "(python -m mba_bot.strategy_pause release) if you really want that; refusing to guess."
            )
    op.drop_index("ix_strategy_pauses_account_strategy", table_name="strategy_pauses")
    op.drop_table("strategy_pauses")
