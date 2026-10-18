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


class LoginRequest(BaseModel):
    email: EmailStr
    password: str


class CustomerOut(BaseModel):
    id: str
    email: str
    created_at: datetime
    has_paid: bool

    model_config = {"from_attributes": True}


class AccountCreateRequest(BaseModel):
    pair: str
    market_type: str
    notification_channel: str
    risk_pct: float | None = Field(default=None, gt=0, le=100)
    api_key: str = Field(min_length=1)
    api_secret: str = Field(min_length=1)
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


class AccountUpdateRequest(BaseModel):
    risk_pct: float | None = Field(default=None, gt=0, le=100)
    is_active: bool | None = None
    pair: str | None = None
    market_type: str | None = None

    @field_validator("pair")
    @classmethod
    def _valid_pair(cls, v: str | None) -> str | None:
        return v if v is None else _validate_choice(v, SUPPORTED_PAIRS, "pair")

    @field_validator("market_type")
    @classmethod
    def _valid_market_type(cls, v: str | None) -> str | None:
        return v if v is None else _validate_choice(v, SUPPORTED_MARKET_TYPES, "market_type")


class LiveBypassRequest(BaseModel):
    accept: bool

    @field_validator("accept")
    @classmethod
    def _must_accept(cls, v: bool) -> bool:
        if not v:
            raise ValueError("accept must be true - this endpoint only does something when you're accepting")
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
    is_active: bool

    # Environment/live-activation state, kept separate from `mode` and
    # from payment (see Customer.has_paid, fetched separately via
    # /api/auth/me) so the frontend never has to conflate "paid for the
    # capability" with "currently connected to Binance production".
    credential_environment: str | None  # "testnet" | "mainnet" | "unknown" | None (no credential on file yet)
    has_mainnet_credential: bool
    live_trading_enabled_at: datetime | None

    # Persisted runtime state (trader.py writes these every scheduler
    # cycle - see models.py) plus a server-computed summary of them, so
    # the frontend never has to re-derive "is this actually working" from
    # raw timestamps itself.
    runtime_status: str  # "paused" | "error" | "trade_open" | "active"
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

    model_config = {"from_attributes": True}


class TradesSummaryOut(BaseModel):
    total_trades: int
    open_trades: int
    closed_trades: int
    net_pnl: float
    win_rate: float | None  # None when there are no closed trades yet


class TelegramLinkOut(BaseModel):
    link_url: str


class PaymentInitOut(BaseModel):
    authorization_url: str
