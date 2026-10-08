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

    # Stripe: set once the customer completes a Checkout session.
    # `subscription_status` mirrors Stripe's own status strings
    # ("active", "past_due", "canceled", ...) rather than inventing our
    # own - the billing webhook writes it directly from the event payload.
    # `TradingAccount.is_active` is what the scheduler actually checks;
    # the billing webhook keeps it in sync with this.
    stripe_customer_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    subscription_status: Mapped[str | None] = mapped_column(String(30), nullable=True)

    account: Mapped["TradingAccount"] = relationship(back_populates="customer", uselist=False, cascade="all, delete-orphan")


class TradingAccount(Base):
    __tablename__ = "trading_accounts"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), nullable=False, unique=True)

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

    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_pnl_summary_date: Mapped[str | None] = mapped_column(String(10), nullable=True)  # "YYYY-MM-DD", UTC

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now, onupdate=_now)

    customer: Mapped["Customer"] = relationship(back_populates="account")
    credential: Mapped["ApiCredential"] = relationship(back_populates="account", uselist=False, cascade="all, delete-orphan")
    trades: Mapped[list["Trade"]] = relationship(back_populates="account", cascade="all, delete-orphan")


class ApiCredential(Base):
    """The customer's own mainnet exchange key. Must be provisioned
    trade-only, no withdrawal permission (enforced on the exchange side at
    key-creation time, and spot-checked at runtime - see exchange.py). Only
    used once the account leaves paper mode; the trial trades against a
    separate platform-level testnet key, not this one."""

    __tablename__ = "api_credentials"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    account_id: Mapped[str] = mapped_column(ForeignKey("trading_accounts.id"), nullable=False, unique=True)
    exchange: Mapped[str] = mapped_column(String(30), default="binance")
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
