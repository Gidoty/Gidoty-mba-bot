"""Pydantic request/response models for the public API.

Pair/market-type/notification-channel membership is validated here
against `config.SUPPORTED_*` so there's one list of allowed values, not
one in the schema and another in the strategy/trader code.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

from ..config import SUPPORTED_MARKET_TYPES, SUPPORTED_NOTIFICATION_CHANNELS, SUPPORTED_PAIRS

# "unknown" is deliberately excluded - it's a migration-only state for
# credentials that predate this concept (see the migration that added
# ApiCredential.environment), never a value a customer can choose. A
# customer always explicitly says testnet or mainnet.
CREDENTIAL_ENVIRONMENTS = ("testnet", "mainnet")


def _validate_choice(value: str, choices: tuple[str, ...], field_name: str) -> str:
    if value not in choices:
        raise ValueError(f"{field_name} must be one of {choices}")
    return value


class SignupRequest(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=72)
    # Both required true at signup - never pre-checked, no way to
    # register without them. See mba_bot.legal for the document
    # versions these are recorded against.
    accept_terms: bool
    acknowledge_privacy: bool

    @field_validator("accept_terms")
    @classmethod
    def _must_accept_terms(cls, v: bool) -> bool:
        if not v:
            raise ValueError("you must accept the Terms of Service to create an account")
        return v

    @field_validator("acknowledge_privacy")
    @classmethod
    def _must_acknowledge_privacy(cls, v: bool) -> bool:
        if not v:
            raise ValueError("you must acknowledge the Privacy Policy to create an account")
        return v


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class AcceptLegalRequest(BaseModel):
    """For an already-authenticated customer catching up to the current
    Terms/Privacy version - e.g. an account created before these
    documents existed. Same all-or-nothing validation as signup."""

    accept_terms: bool
    acknowledge_privacy: bool

    @field_validator("accept_terms")
    @classmethod
    def _must_accept_terms(cls, v: bool) -> bool:
        if not v:
            raise ValueError("accept_terms must be true")
        return v

    @field_validator("acknowledge_privacy")
    @classmethod
    def _must_acknowledge_privacy(cls, v: bool) -> bool:
        if not v:
            raise ValueError("acknowledge_privacy must be true")
        return v


class CustomerOut(BaseModel):
    id: str
    email: str
    created_at: datetime
    has_paid: bool
    # Whether the CURRENT Terms+Privacy version is accepted - the
    # frontend blocks everything past login on this until it's true.
    legal_acceptance_current: bool

    model_config = {"from_attributes": True}


class AccountCreateRequest(BaseModel):
    pair: str
    market_type: str
    notification_channel: str
    risk_pct: float | None = Field(default=None, gt=0, le=100)
    # Both optional: paper-mode trading never reads an account's own
    # credential (it always uses the shared platform testnet keys - see
    # trader.py's _resolve_credentials), so a brand-new, unpaid account
    # has no reason to be asked for a Binance key of any kind at
    # signup - Mainnet credential entry only becomes available later,
    # after payment, through the dedicated Connect Binance step
    # (PUT /api/account/credentials). Provided only for callers that
    # still want to set one up front (e.g. an admin script).
    api_key: str | None = Field(default=None, min_length=1)
    api_secret: str | None = Field(default=None, min_length=1)
    # Defaults to the safe choice: a credential nobody explicitly tagged
    # mainnet can never be used for live trading, no matter what else is
    # true about the account (see models.py's has_mainnet_credential).
    environment: str = "testnet"

    @field_validator("pair")
    @classmethod
    def _valid_pair(cls, v: str) -> str:
        return _validate_choice(v, SUPPORTED_PAIRS, "pair")

    @field_validator("market_type")
    @classmethod
    def _valid_market_type(cls, v: str) -> str:
        return _validate_choice(v, SUPPORTED_MARKET_TYPES, "market_type")

    @field_validator("notification_channel")
    @classmethod
    def _valid_channel(cls, v: str) -> str:
        return _validate_choice(v, SUPPORTED_NOTIFICATION_CHANNELS, "notification_channel")

    @field_validator("environment")
    @classmethod
    def _valid_environment(cls, v: str) -> str:
        return _validate_choice(v, CREDENTIAL_ENVIRONMENTS, "environment")


class CredentialUpdateRequest(BaseModel):
    api_key: str = Field(min_length=1)
    api_secret: str = Field(min_length=1)
    environment: str

    @field_validator("environment")
    @classmethod
    def _valid_environment(cls, v: str) -> str:
        return _validate_choice(v, CREDENTIAL_ENVIRONMENTS, "environment")


PAIR_SELECTION_MODES = ("manual", "auto")


class AccountUpdateRequest(BaseModel):
    risk_pct: float | None = Field(default=None, gt=0, le=100)
    pair: str | None = None
    market_type: str | None = None
    # How much of the customer's available Binance balance the bot may
    # use - see models.py's TradingAccount.allocated_capital. Omitting
    # this field means "don't change it", same convention every other
    # optional field on this request already uses; there is no way to
    # explicitly clear it back to "not configured" through this
    # endpoint, by design - decision #10 of the capital-allocation spec
    # treats "not configured" as a safety-relevant state, not something
    # to toggle back into casually.
    allocated_capital: float | None = Field(default=None, gt=0)
    pair_selection_mode: str | None = None

    @field_validator("pair")
    @classmethod
    def _valid_pair(cls, v: str | None) -> str | None:
        return v if v is None else _validate_choice(v, SUPPORTED_PAIRS, "pair")

    @field_validator("market_type")
    @classmethod
    def _valid_market_type(cls, v: str | None) -> str | None:
        return v if v is None else _validate_choice(v, SUPPORTED_MARKET_TYPES, "market_type")

    @field_validator("pair_selection_mode")
    @classmethod
    def _valid_pair_selection_mode(cls, v: str | None) -> str | None:
        return v if v is None else _validate_choice(v, PAIR_SELECTION_MODES, "pair_selection_mode")


class LiveBypassRequest(BaseModel):
    accept: bool

    @field_validator("accept")
    @classmethod
    def _must_accept(cls, v: bool) -> bool:
        if not v:
            raise ValueError("accept must be true - this endpoint only does something when you're accepting")
        return v


class EnableLiveTradingRequest(BaseModel):
    """A dedicated field, not the generic `accept` LiveBypassRequest uses
    for other endpoints - this one specifically means "I acknowledge the
    Trading Risk Disclosure", and is required true in addition to (never
    instead of) the four existing paid/eligible/enabled/mainnet gates."""

    accept_risk_disclosure: bool

    @field_validator("accept_risk_disclosure")
    @classmethod
    def _must_accept_risk_disclosure(cls, v: bool) -> bool:
        if not v:
            raise ValueError(
                "accept_risk_disclosure must be true - enabling live trading requires acknowledging the "
                "Trading Risk Disclosure"
            )
        return v


class StartBotRequest(BaseModel):
    """The final, separate explicit action - enabling live trading
    authorizes going live, it does not start anything by itself. See
    onboarding.resolve_trading_mode's fifth gate and
    api/routers/account.py's start_bot_route for the full prerequisite
    chain this is checked against server-side."""

    confirm: bool

    @field_validator("confirm")
    @classmethod
    def _must_confirm(cls, v: bool) -> bool:
        if not v:
            raise ValueError("confirm must be true - this endpoint only does something when you're confirming")
        return v


class AccountOut(BaseModel):
    id: str
    bot_code: str | None
    pair: str
    market_type: str
    notification_channel: str
    notification_target: str | None
    risk_pct: float
    mode: str  # "paper" | "live"
    trial_ends_at: datetime
    paper_balance: float
    live_bypass_accepted_at: datetime | None
    # No customer-facing control sets this anymore (Pause was removed -
    # see api/routers/account.py); kept read-only here purely so the
    # frontend can still see the internal/admin kill switch if it's ever
    # used, same as `runtime_status`'s "paused" value.
    is_active: bool

    # Environment/live-activation state, kept separate from `mode` and
    # from payment (see Customer.has_paid, fetched separately via
    # /api/auth/me) so the frontend never has to conflate "paid for the
    # capability" with "currently connected to Binance production".
    credential_environment: str | None  # "testnet" | "mainnet" | "unknown" | None (no credential on file yet)
    has_mainnet_credential: bool
    live_trading_enabled_at: datetime | None
    # The explicit "Start Bot" action - set only by POST
    # /api/account/start-bot, never by enabling live trading itself.
    # See onboarding.resolve_trading_mode's fifth gate.
    bot_started_at: datetime | None
    allocated_capital: float | None
    pair_selection_mode: str  # "manual" | "auto"

    # Persisted runtime state (trader.py writes these every scheduler
    # cycle - see models.py) plus a server-computed summary of them, so
    # the frontend never has to re-derive "is this actually working" from
    # raw timestamps itself.
    runtime_status: str  # "paused" | "not_started" | "attention" | "error" | "trade_open" | "active"
    last_scanned_at: datetime | None
    last_signal: str | None  # "none" | "bullish_crossover" | "bearish_crossover"
    last_signal_at: datetime | None
    last_error: str | None  # already a short, customer-safe message - see exchange.safe_error_message
    last_error_at: datetime | None

    model_config = {"from_attributes": True}


class TradeOut(BaseModel):
    id: str
    side: str
    is_paper: bool
    entry_price: float
    quantity: float
    stop_loss_price: float
    exit_price: float | None
    pnl: float | None
    close_reason: str | None
    status: str
    opened_at: datetime
    closed_at: datetime | None
    # Live trades: "protected" once the stop-loss is resting at Binance and
    # verified. Display-only; nothing in the API can alter it.
    protection_status: str | None = None

    model_config = {"from_attributes": True}


class TradesSummaryOut(BaseModel):
    total_trades: int
    open_trades: int
    closed_trades: int
    net_pnl: float  # all-time, scoped by the `is_paper` filter if one was passed
    win_rate: float | None  # None when there are no closed trades yet
    # Windowed P&L, same `is_paper` scope as net_pnl - lets the dashboard
    # show Today/7D/30D without re-deriving date math client-side, and
    # without ever mixing paper and live figures into one number.
    pnl_today: float
    pnl_7d: float
    pnl_30d: float
    pnl_90d: float


class TelegramLinkOut(BaseModel):
    link_url: str


class PaymentInitOut(BaseModel):
    authorization_url: str


class BalanceOut(BaseModel):
    """Live Binance balance, fetched on demand - never persisted, never
    available until an account is actually LIVE. `available_balance` is
    the free (not locked in orders) balance of the pair's quote currency,
    same figure trader.py itself sizes positions off."""

    quote_currency: str
    available_balance: float


class PositionOut(BaseModel):
    """The account's one open position, if any, with a live current
    price layered on top of the persisted Trade row. `current_price`/
    `unrealized_pnl` are best-effort - None if the live price couldn't
    be fetched just now, never fabricated."""

    has_position: bool
    pair: str | None = None
    side: str | None = None  # "long" | "short"
    is_paper: bool | None = None
    entry_price: float | None = None
    current_price: float | None = None
    quantity: float | None = None
    stop_loss_price: float | None = None
    unrealized_pnl: float | None = None
    opened_at: datetime | None = None
    # Live trades only: True once the stop-loss is resting at Binance and
    # verified. None for paper trades. Display-only - nothing in the API
    # lets a customer touch the protective order.
    protected: bool | None = None


class CredentialTestOut(BaseModel):
    ok: bool
    message: str
