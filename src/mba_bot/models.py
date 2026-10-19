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

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint, text
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
    # "manual" (default, preserves original single-configured-pair
    # behavior exactly) | "auto" (trader.py may reassign `pair` itself,
    # between trades only - see trader.py's open-trade guard around
    # auto-selection, a scheduler-side check independent of the
    # API-layer one in api/routers/account.py).
    pair_selection_mode: Mapped[str] = mapped_column(String(10), nullable=False, default="manual", server_default="manual")
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

    # The explicit "Start Bot" action - a SIXTH concept, separate from
    # all of the above. live_trading_enabled_at means "I accept the
    # risk and am authorized to go live"; bot_started_at means "I am
    # explicitly telling the bot to begin operating now." Enabling live
    # trading never sets this by itself. This is the fifth and final
    # condition resolve_trading_mode (onboarding.py) requires before it
    # will ever return LIVE - see that function for the full chain.
    # Cleared alongside live_trading_enabled_at whenever the stored
    # credential stops being mainnet-tagged (api/routers/account.py),
    # same reasoning: a "started" flag with nothing valid backing it is
    # a stale, confusing state, not a safety hole by itself (the mode
    # gate still blocks LIVE regardless), but there is no reason to
    # leave it lying around.
    bot_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # How much of the customer's available Binance balance the bot may
    # use as its capital base for position sizing - NOT a transfer, the
    # funds never leave the customer's Binance account. None means
    # "not yet configured" and must NEVER be treated as "use everything
    # available" (see trader.py's _maybe_enter, which fails closed - no
    # entry, not "fall back to full balance" - when this is None for a
    # live-mode account). Enforced as a ceiling, not a guarantee: the
    # live balance is still read fresh from Binance at order time, and
    # the smaller of the two actually bounds position sizing.
    allocated_capital: Mapped[float | None] = mapped_column(Float, nullable=True)

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

    # Set (once, durably) the first time the stored Binance key is found to
    # have withdrawal permission. While set: NO new live entry, ever, even if
    # the permission is later removed on Binance - only replacing the
    # credential (api/routers/account.py) clears it. An already-open position
    # keeps being managed for risk reduction only. Being a column, it survives
    # restarts and makes the customer alert fire once per episode.
    withdrawal_blocked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

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


class LegalAcceptance(Base):
    """One row per acceptance event - terms, privacy, or risk_disclosure -
    never overwritten and never backdated. A customer with no row for a
    given (document_type, version) has NOT accepted it, full stop; that
    includes every customer who existed before this table did, since the
    migration that introduces it adds an empty table, not fabricated
    history (see legal.py, which is the only module that reads/writes
    this and the single place "is this customer's acceptance current"
    is decided). No API key, secret, or other sensitive account data is
    ever stored here - just which document, which version, and when."""

    __tablename__ = "legal_acceptances"
    __table_args__ = (
        UniqueConstraint(
            "customer_id", "document_type", "version", name="uq_legal_acceptance_customer_document_version"
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=_uuid)
    customer_id: Mapped[str] = mapped_column(ForeignKey("customers.id"), nullable=False)
    document_type: Mapped[str] = mapped_column(String(20), nullable=False)  # "terms" | "privacy" | "risk_disclosure"
    version: Mapped[str] = mapped_column(String(20), nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)


# --- Trade lifecycle -------------------------------------------------------
#
# pending     live only. The intent row, committed BEFORE any order is sent
#             (write-ahead). The entry order may or may not exist at Binance;
#             only the reconciler/lifecycle resolves it. Never re-sent blindly.
# protecting  live only. Entry fill is known and persisted; the exchange-side
#             protective stop is not yet verified to exist.
# open        paper trade, or a live trade whose protective stop was verified.
# closing     exit lease: exactly one worker owns closing this trade.
# attention   needs the reconciler (and possibly the operator): unprotected,
#             unresolved entry, residual exposure, etc. Blocks new entries.
# closed      terminal, the trade had exposure and is now flat.
# void        terminal, the entry never happened (no exposure ever existed).
TRADE_PENDING = "pending"
TRADE_PROTECTING = "protecting"
TRADE_OPEN = "open"
TRADE_CLOSING = "closing"
TRADE_ATTENTION = "attention"
TRADE_CLOSED = "closed"
TRADE_VOID = "void"
NON_TERMINAL_TRADE_STATUSES = (TRADE_PENDING, TRADE_PROTECTING, TRADE_OPEN, TRADE_CLOSING, TRADE_ATTENTION)
TERMINAL_TRADE_STATUSES = (TRADE_CLOSED, TRADE_VOID)

PROTECTION_PENDING = "pending"
PROTECTION_PROTECTED = "protected"
PROTECTION_MISSING = "missing"
PROTECTION_TRIGGERED = "triggered"
PROTECTION_CANCELLED = "cancelled"


class Trade(Base):
    __tablename__ = "trades"
    __table_args__ = (
        # The database-level one-position rule: at most ONE non-terminal
        # trade per account, regardless of what any code path believes.
        # A second concurrent entry (double scheduler, retry after an
        # uncertain order) fails at the write-ahead INSERT, before any
        # order is sent. Partial unique index - supported by SQLite and
        # PostgreSQL, the only two databases this project targets.
        Index(
            "uq_trades_one_nonterminal_per_account",
            "account_id",
            unique=True,
            sqlite_where=text("status NOT IN ('closed', 'void')"),
            postgresql_where=text("status NOT IN ('closed', 'void')"),
        ),
    )

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

    # See the TRADE_* constants above. Default "open" is the paper-trade
    # path; live entries always start at "pending" (write-ahead).
    status: Mapped[str] = mapped_column(String(10), default=TRADE_OPEN)

    # Unified pair the trade was opened on (e.g. "BTC/USDT"), so managing
    # it never depends on whatever account.pair says later. NULL on rows
    # from before this column existed; callers fall back to account.pair.
    pair: Mapped[str | None] = mapped_column(String(20), nullable=True)

    # Exchange identifiers. Client order ids are NOT stored: they are a pure
    # function of trade.id (lifecycle.client_id), which is what makes every
    # retry idempotent. These hold the exchange-assigned ids for reference.
    entry_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    exit_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    protective_order_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # Which protective mechanism is resting: "stop_market_close" |
    # "stop_market_reduce" (Futures), "stop_loss" | "stop_loss_limit" (Spot).
    protective_kind: Mapped[str | None] = mapped_column(String(24), nullable=True)
    # Spot STOP_LOSS_LIMIT only: the limit price below the trigger.
    protective_limit_price: Mapped[float | None] = mapped_column(Float, nullable=True)
    # NULL for paper trades and rows from before protective stops existed.
    protection_status: Mapped[str | None] = mapped_column(String(12), nullable=True)
    protection_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Short machine code for why a trade is in "attention" (never shown raw
    # to customers, never contains exchange text).
    attention_reason: Mapped[str | None] = mapped_column(String(40), nullable=True)
    # Spot only: base-asset commission taken from the entry fill. The
    # protected/exitable quantity is filled quantity minus this.
    entry_fee_base: Mapped[float | None] = mapped_column(Float, nullable=True)
    # Counters that make every retry idempotent without storing client ids:
    # the client order id of the Nth protective stop / exit order is a pure
    # function of (trade.id, role, N). The counter is committed BEFORE the
    # order is sent, so after any crash or lost response the same N is
    # looked up (and adopted or safely re-sent) rather than a fresh id
    # being invented. N only advances when the previous attempt is known
    # to be terminal. NULL (legacy rows) reads as 0.
    protection_attempt: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0, server_default="0")
    exit_attempt: Mapped[int | None] = mapped_column(Integer, nullable=True, default=0, server_default="0")

    opened_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=_now)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    account: Mapped["TradingAccount"] = relationship(back_populates="trades")
