"""Response models for the admin routes. Every model lists its allowed fields explicitly (FastAPI drops anything else), and none has a field for a password hash, key, secret, token, chat id,
payment reference or address (tests/test_admin_dashboard.py serialises every response and fails on a forbidden name or an email-like string outside the reveal)."""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field


class AdminLoginRequest(BaseModel):
    code: str = Field(min_length=1, max_length=32)


class AdminLoginOut(BaseModel):
    ok: bool
    expires_in_seconds: int


class AdminMeOut(BaseModel):
    is_admin: bool
    session: bool


class CustomersPanel(BaseModel):
    total: int
    active: int
    paid: int
    new_7_days: int
    new_30_days: int


class BotsPanel(BaseModel):
    practice: int
    live: int
    live_stopped: int
    inactive: int
    total: int


class HealthPanel(BaseModel):
    bots: int
    oldest_last_scan_age_seconds: int | None
    bots_with_error_now: int
    bots_not_scanned_15_min: int
    open_trades: int
    open_trades_protection_not_healthy: int
    kill_switch: str
    last_deploy_commit: str | None


class ResultGroup(BaseModel):
    mode: str
    strategy: str
    closed_trades: int
    wins: int
    losses: int
    gross_pnl: float
    fees: float | None
    trades_with_fees: int
    fees_complete: bool
    funding: float | None = None
    trades_with_funding: int = 0
    funding_complete: bool = False
    net_pnl: float | None
    simulated: bool
    label: str
    mode_label: str


class ResultBot(BaseModel):
    bot_code: str
    pair: str | None
    market_type: str | None
    groups: list[ResultGroup]


class ResultsPanel(BaseModel):
    window_days: int | None
    funding_note: str
    totals: list[ResultGroup]
    bots: list[ResultBot]


class CustomerRow(BaseModel):
    ref: str | None
    masked_email: str
    account_prefix: str | None
    pair: str | None
    market_type: str | None
    mode: str | None
    customer_active: bool
    paid: bool
    created_at: datetime | None
    last_scan_age_seconds: int | None
    has_error: bool
    last_error_at: datetime | None


class CustomerList(BaseModel):
    page: int
    page_size: int
    total: int
    items: list[CustomerRow]


class RevealRequest(BaseModel):
    ref: str = Field(min_length=1, max_length=36)
    reason: str = Field(max_length=400)


class RevealOut(BaseModel):
    email: str
    reveals_left_today: int


class SkipReason(BaseModel):
    reason: str
    skips: int
    accounts: int


class SkipsPanel(BaseModel):
    days: int
    reasons: list[SkipReason]


class OpenTradeRow(BaseModel):
    bot_code: str
    pair: str | None = None
    strategy: str
    side: str
    status: str
    opened_at: datetime
    entry_price: float
    initial_stop_price: float | None = None
    stop_loss_price: float
    peak_price: float | None = None
    peak_r: float | None = None


class OpenTradesPanel(BaseModel):
    trades: list[OpenTradeRow]


class PaymentRow(BaseModel):
    provider: str
    status: str
    currency: str
    count: int
    amount_minor: int


class BillingSubscriptions(BaseModel):
    active: int
    grace: int
    expired: int
    cancelled: int


class BillingSwitches(BaseModel):
    subscription_billing_enabled: bool
    paystack_verify_mode: str
    nowpayments_enabled: bool
    nowpayments_ipn_mode: str


class BillingPanelOut(BaseModel):
    subscriptions: BillingSubscriptions
    expiring_in_7_days: int
    payments_30_days: list[PaymentRow]
    failed_or_mismatched_30_days: int
    switches: BillingSwitches
