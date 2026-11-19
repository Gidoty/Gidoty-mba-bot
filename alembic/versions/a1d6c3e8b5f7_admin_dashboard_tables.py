"""admin_totp and admin_audit (admin dashboard, docs/security/admin_dashboard.md)

Revision ID: a1d6c3e8b5f7
Revises: d9b5f7c2e4a6
Create Date: 2026-11-19

Additive, idempotent and reversible. NOT run by Claude; the owner runs it after a backup (restart gate first). Two new tables, nothing else changes; nothing reads or writes them unless
ADMIN_DASHBOARD_ENABLED is on. The downgrade drops both tables (the TOTP enrolment and the audit history are lost).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "a1d6c3e8b5f7"
down_revision: Union[str, Sequence[str], None] = "d9b5f7c2e4a6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    names = sa.inspect(op.get_bind()).get_table_names()
    if "admin_totp" not in names:
        op.create_table(
            "admin_totp",
            sa.Column("customer_id", sa.String(36), sa.ForeignKey("customers.id"), primary_key=True),
            sa.Column("secret_enc", sa.Text(), nullable=False),
            sa.Column("recovery_hashes", sa.Text(), nullable=False),
            sa.Column("last_step", sa.Integer(), nullable=False),
            sa.Column("failures", sa.Integer(), nullable=False),
            sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
            sa.Column("enrolled_at", sa.DateTime(timezone=True), nullable=False),
        )
    if "admin_audit" not in names:
        op.create_table(
            "admin_audit",
            sa.Column("id", sa.String(36), primary_key=True),
            sa.Column("customer_id", sa.String(36), nullable=False),
            sa.Column("at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("action", sa.String(24), nullable=False),
            sa.Column("path", sa.String(80), nullable=False),
            sa.Column("status", sa.Integer(), nullable=False),
            sa.Column("detail", sa.String(200), nullable=True),
        )
        op.create_index("ix_admin_audit_customer_at", "admin_audit", ["customer_id", "at"])


def downgrade() -> None:
    names = sa.inspect(op.get_bind()).get_table_names()
    if "admin_audit" in names:
        op.drop_table("admin_audit")
    if "admin_totp" in names:
        op.drop_table("admin_totp")
