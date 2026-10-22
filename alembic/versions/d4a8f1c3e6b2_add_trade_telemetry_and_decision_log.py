"""add trade_telemetry and decision_log (research telemetry, Phase 0)

Revision ID: d4a8f1c3e6b2
Revises: c7d2e5f8a1b3
Create Date: 2026-10-23 00:00:00.000000

Two NEW tables only. No existing table or column is touched, so the
migration cannot affect any running behaviour; both tables stay empty until
TRADE_TELEMETRY is switched on.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "d4a8f1c3e6b2"
down_revision: Union[str, Sequence[str], None] = "c7d2e5f8a1b3"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "trade_telemetry",
        sa.Column("trade_id", sa.String(36), sa.ForeignKey("trades.id"), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("schema_version", sa.Integer(), nullable=True),
        sa.Column("strategy_id", sa.String(40), nullable=False),
        sa.Column("strategy_version", sa.String(16), nullable=False),
        sa.Column("strategy_params_json", sa.Text(), nullable=True),
        sa.Column("mode", sa.String(8), nullable=False),
        sa.Column("market_type", sa.String(10), nullable=False),
        sa.Column("pair", sa.String(20), nullable=False),
        sa.Column("pair_selection_mode", sa.String(10), nullable=True),
        sa.Column("timeframe", sa.String(8), nullable=True),
        sa.Column("signal_type", sa.String(24), nullable=False),
        sa.Column("signal_reason", sa.String(120), nullable=True),
        sa.Column("signal_price", sa.Float(), nullable=True),
        sa.Column("candle_ts", sa.BigInteger(), nullable=True),
        sa.Column("atr", sa.Float(), nullable=True),
        sa.Column("atr_pct", sa.Float(), nullable=True),
        sa.Column("fast_ema", sa.Float(), nullable=True),
        sa.Column("slow_ema", sa.Float(), nullable=True),
        sa.Column("effective_capital", sa.Float(), nullable=True),
        sa.Column("risk_pct", sa.Float(), nullable=True),
        sa.Column("planned_risk_amount", sa.Float(), nullable=True),
        sa.Column("planned_quantity", sa.Float(), nullable=True),
        sa.Column("planned_notional", sa.Float(), nullable=True),
        sa.Column("stop_price", sa.Float(), nullable=True),
        sa.Column("stop_distance_pct", sa.Float(), nullable=True),
        sa.Column("account_leverage", sa.Float(), nullable=True),
        sa.Column("auto_rank", sa.Integer(), nullable=True),
        sa.Column("auto_score", sa.Float(), nullable=True),
        sa.Column("auto_candidates", sa.Integer(), nullable=True),
        sa.Column("features_json", sa.Text(), nullable=True),
        sa.Column("entry_fee_quote", sa.Float(), nullable=True),
        sa.Column("exit_fee_quote", sa.Float(), nullable=True),
        sa.Column("funding_quote", sa.Float(), nullable=True),
        sa.Column("costs_enriched_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "decision_log",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("ts", sa.DateTime(timezone=True), nullable=True),
        sa.Column("account_id", sa.String(36), nullable=False),
        sa.Column("mode", sa.String(8), nullable=False),
        sa.Column("market_type", sa.String(10), nullable=False),
        sa.Column("pair", sa.String(20), nullable=True),
        sa.Column("strategy_id", sa.String(40), nullable=False),
        sa.Column("stage", sa.String(16), nullable=False),
        sa.Column("outcome", sa.String(10), nullable=False),
        sa.Column("reason_code", sa.String(48), nullable=False),
        sa.Column("signal", sa.String(24), nullable=True),
        sa.Column("price", sa.Float(), nullable=True),
        sa.Column("candle_ts", sa.BigInteger(), nullable=True),
        sa.Column("trade_id", sa.String(36), nullable=True),
        sa.Column("detail_json", sa.Text(), nullable=True),
    )
    op.create_index("ix_decision_log_ts", "decision_log", ["ts"])
    op.create_index("ix_decision_log_account_ts", "decision_log", ["account_id", "ts"])
    op.create_index("ix_decision_log_reason", "decision_log", ["reason_code"])


def downgrade() -> None:
    op.drop_index("ix_decision_log_reason", table_name="decision_log")
    op.drop_index("ix_decision_log_account_ts", table_name="decision_log")
    op.drop_index("ix_decision_log_ts", table_name="decision_log")
    op.drop_table("decision_log")
    op.drop_table("trade_telemetry")
