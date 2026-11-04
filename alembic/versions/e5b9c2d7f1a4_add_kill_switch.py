"""add kill_switch and kill_switch_events (operator kill switch)

Revision ID: e5b9c2d7f1a4
Revises: d4a8f1c3e6b2
Create Date: 2026-11-04 00:00:00.000000

Two NEW tables only; no existing table or column is touched. The state row is seeded RELEASED, so applying this changes
no behaviour until the owner engages the switch (python -m mba_bot.killswitch engage ...).

Idempotent: init_db() runs create_all() at every service start, so once this code is deployed the tables may already
exist (and the first-run seed may already have created the row). Anything already there is left alone.

Downgrade drops both tables. Code that predates the kill switch ignores the table, so a rollback RESUMES trading: engage
the break-glass file first if it must stay stopped (docs/safety/kill_switch.md).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "e5b9c2d7f1a4"
down_revision: Union[str, Sequence[str], None] = "d4a8f1c3e6b2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    bind = op.get_bind()
    insp = sa.inspect(bind)
    if not insp.has_table("kill_switch"):
        op.create_table(
            "kill_switch",
            sa.Column("id", sa.Integer(), primary_key=True),
            sa.Column("engaged", sa.Boolean(), nullable=False),
            sa.Column("reason", sa.String(200), nullable=True),
            sa.Column("changed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("changed_by", sa.String(60), nullable=True),
            sa.CheckConstraint("id = 1", name="ck_kill_switch_single_row"),
        )
    if not insp.has_table("kill_switch_events"):
        op.create_table(
            "kill_switch_events",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("action", sa.String(12), nullable=False),
            sa.Column("actor", sa.String(60), nullable=False),
            sa.Column("reason", sa.String(200), nullable=True),
            sa.Column("prev_engaged", sa.Boolean(), nullable=True),
        )
    insp = sa.inspect(bind)
    if "ix_kill_switch_events_at" not in {i["name"] for i in insp.get_indexes("kill_switch_events")}:
        op.create_index("ix_kill_switch_events_at", "kill_switch_events", ["at"])
    # Seed RELEASED only on a first run: no row and no audit event ever written.
    n_rows = bind.execute(sa.text("SELECT COUNT(*) FROM kill_switch")).scalar()
    n_events = bind.execute(sa.text("SELECT COUNT(*) FROM kill_switch_events")).scalar()
    if not n_rows and not n_events:
        import uuid
        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        state = sa.table(
            "kill_switch",
            sa.column("id", sa.Integer), sa.column("engaged", sa.Boolean), sa.column("reason", sa.String),
            sa.column("changed_at", sa.DateTime(timezone=True)), sa.column("changed_by", sa.String),
        )
        events = sa.table(
            "kill_switch_events",
            sa.column("id", sa.String), sa.column("at", sa.DateTime(timezone=True)), sa.column("action", sa.String),
            sa.column("actor", sa.String), sa.column("reason", sa.String), sa.column("prev_engaged", sa.Boolean),
        )
        bind.execute(sa.insert(state).values(id=1, engaged=False, reason=None, changed_at=now, changed_by="seed"))
        bind.execute(
            sa.insert(events).values(
                id=str(uuid.uuid4()), at=now, action="seed", actor="seed", reason="migration", prev_engaged=None
            )
        )


def downgrade() -> None:
    insp = sa.inspect(op.get_bind())
    if insp.has_table("kill_switch_events"):
        if "ix_kill_switch_events_at" in {i["name"] for i in insp.get_indexes("kill_switch_events")}:
            op.drop_index("ix_kill_switch_events_at", table_name="kill_switch_events")
        op.drop_table("kill_switch_events")
    if insp.has_table("kill_switch"):
        op.drop_table("kill_switch")
