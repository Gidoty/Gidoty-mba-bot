"""SQLAlchemy ORM models for the multi-tenant schema.

One Customer -> one TradingAccount -> one ApiCredential, many Trades.
Kept to a single account per customer for now, matching the "set it up
once at signup" onboarding flow described in the product spec; nothing
here blocks adding multiple accounts per customer later since the FK
lives on TradingAccount, not the other way round.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, String, Text
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


def _uuid() -> str:
    return str(uuid.uuid4())


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Customer(Base):
    __tablename__ = "customers"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    email: Mapped[str] = mapped_column(String(255), unique=True, nullable=False)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)

    # Lifetime access, unlocked one of two ways: the Paystack webhook
    # (apply_payment_event) on a real payment, or the operator running
    # `scripts/mark_customer_paid.py` for anyone who paid outside
    # Paystack (bank transfer, cash, ...) - both just set this flag the
    # same way. A new account is locked by default: there's no free
    # trial here, by the operator's choice - see db.list_active_accounts,
    # which the scheduler uses and which requires `has_paid` before it
    # will trade an account at all, paper or live.
    has_paid: Mapped[bool] = mapped_column(Boolean, default=False)
    # The Paystack transaction reference, or a free-text note (e.g. a
    # bank transfer reference) when unlocked manually.
    paystack_reference: Mapped[str | None] = mapped_column(String(100), nullable=True)

    account: Mapped["TradingAccount"] = relationship(back_populates="customer", uselist=False, cascade="all, delete-orphan")


class TradingAccount(Base):
    __tablename__ = "trading_accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), nullable=False, unique=True)

    # Short support-facing code ("MBA-7F3K9Q"), always set by application
    # code at creation time (see botcode.py) - nullable at the DB level
    # only because SQLite can't cheaply add a NOT NULL column to a table
    # that may already have rows (see the migration that introduced it).
    bot_code: Mapped[str | None] = mapped_column(String(20), nullable=True, unique=True)

    pair: Mapped[str] = mapped_column(String(20), nullable=False)
    market_type: Mapped[str] = mapped_column(String(10), nullable=False)
    notification_channel: Mapped[str] = mapped_column(String(20), nullable=False)
    # Null until the channel is actually linked (e.g. Telegram: the
    # customer has to tap a deep link before we have a chat_id -
    # see telegram_link_token below and api/routers/telegram.py).
    notification_target: Mapped[str | None] = mapped_column(String(255), nullable=True)
    # One-time token embedded in the Telegram deep link
    # (t.me/<bot>?start=<token>); cleared once the webhook resolves it to
    # a chat_id and fills in notification_target above.
    telegram_link_token: Mapped[str | None] = mapped_column(String(64), nullable=True, unique=True)

    risk_pct: Mapped[float] = mapped_column(Float, default=1.0)

    # Paper-trading trial. `paper_balance` is the customer's simulated
    # equity while in the trial - trades placed during the trial execute
    # against a platform-level Binance testnet key (see .env.example),
    # but each customer's simulated P&L is tracked independently here so
    # testnet balances don't need to be provisioned per customer.
    trial_ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    paper_balance: Mapped[float] = mapped_column(Float, default=10_000.0)
    live_bypass_accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    live_bypass_disclosure_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    # A SEPARATE gate from the bypass above. live_bypass_accepted_at only
    # ever meant "don't make me wait out the 7-day trial" - it says
    # nothing about which Binance environment to actually trade against.
    # live_trading_enabled_at is the one and only switch that lets
    # resolve_trading_mode (onboarding.py) ever return LIVE: paying,
    # finishing the trial, or accepting the bypass are all eligibility
    # conditions, never sufficient on their own. Cleared automatically
    # whenever the stored credential stops being mainnet-tagged (see
    # api/routers/account.py's credential-update endpoint) so swapping
    # back to a testnet key can't leave this account live-authorized
    # against nothing valid.
    live_trading_enabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    live_trading_disclosure_text: Mapped[str | None] = mapped_column(Text, nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_pnl_summary_date: Mapped[str | None] = mapped_column(String(10), nullable=True)  # "YYYY-MM-DD", UTC

    # Persisted runtime state, written every scheduler cycle (trader.py) so
    # the dashboard can show real status instead of assuming the bot is
    # working. last_scanned_at updates on every attempt, success or not;
    # last_signal/last_signal_at only update on a successful evaluation;
    # last_error/last_error_at are set on failure and cleared back to None
    # on the next success - "error" is deliberately not sticky forever,
    # it reflects the most recent cycle only. All customer-facing, so
    # last_error is always a short sanitized message (see
    # exchange.safe_error_message), never a raw exception or traceback.
    last_scanned_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_signal: Mapped[str | None] = mapped_column(String(30), nullable=True)
    last_signal_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    last_error_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Set once, the first time this account's mode ever resolves to LIVE,
    # so the one-time "you're now live" Telegram message never repeats -
    # not cleared by pausing/resuming or by a later pair/market change.
    live_notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    customer: Mapped["Customer"] = relationship(back_populates="account")
    credential: Mapped["ApiCredential"] = relationship(back_populates="account", uselist=False, cascade="all, delete-orphan")
    trades: Mapped[list["Trade"]] = relationship(back_populates="account", cascade="all, delete-orphan")

    @property
    def has_mainnet_credential(self) -> bool:
        """The one check resolve_trading_mode (onboarding.py) trusts to
        ever allow LIVE - kept in exactly one place so trader.py and
        api/routers/account.py can't drift into checking it differently."""
        return self.credential is not None and self.credential.environment == "mainnet"


class ApiCredential(Base):
    """The customer's own exchange key - NOT necessarily mainnet, despite
    this class historically assuming that. `environment` says which
    Binance system this key is actually registered against ("testnet" |
    "mainnet" | "unknown"), set explicitly by the customer, never
    inferred. This is the only thing resolve_trading_mode trusts to ever
    allow LIVE: a key tagged anything other than "mainnet" can never be
    used for live trading, no matter what else is true about the account
    (see trader.py's _resolve_credentials and onboarding.py). Must be
    provisioned trade-only, no withdrawal permission (enforced on the
    exchange side at key-creation time, and spot-checked at runtime - see
    exchange.py)."""

    __tablename__ = "api_credentials"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(ForeignKey("trading_accounts.id"), nullable=False, unique=True)
    exchange: Mapped[str] = mapped_column(String(30), default="binance")
    # "unknown" is the deliberately-unprivileged default: a credential
    # that was never explicitly tagged must never be treated as mainnet.
    # See the migration that added this column for why existing rows
    # before this concept existed are backfilled to "unknown", not
    # "testnet" or "mainnet" - we genuinely don't know which they are.
    environment: Mapped[str] = mapped_column(String(20), nullable=False, default="unknown", server_default="unknown")
    encrypted_api_key: Mapped[str] = mapped_column(Text, nullable=False)
    encrypted_api_secret: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)

    account: Mapped["TradingAccount"] = relationship(back_populates="credential")


class Trade(Base):
    __tablename__ = "trades"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(ForeignKey("trading_accounts.id"), nullable=False)

    side: Mapped[str] = mapped_column(String(10), nullable=False)  # "long" | "short"
    is_paper: Mapped[bool] = mapped_column(Boolean, nullable=False)

    entry_price: Mapped[float] = mapped_column(Float, nullable=False)
    quantity: Mapped[float] = mapped_column(Float, nullable=False)
    stop_loss_price: Mapped[float] = mapped_column(Float, nullable=False)

    exit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    pnl: Mapped[float | None] = mapped_column(Float, nullable=True)
    close_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)  # "stop_loss" | "crossover_exit"

    status: Mapped[str] = mapped_column(String(10), default="open")  # "open" | "closed"

    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    account: Mapped["TradingAccount"] = relationship(back_populates="trades")
