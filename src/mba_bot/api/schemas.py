"""Pydantic request/response models for the public API.

Pair/market-type/notification-channel membership is validated here
against `config.SUPPORTED_*` so there's one list of allowed values, not
one in the schema and another in the strategy/trader code.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, field_validator

from ..config import SUPPORTED_MARKET_TYPES, SUPPORTED_NOTIFICATION_CHANNELS, SUPPORTED_PAIRS


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

    model_config = {"from_attributes": True}


class AccountCreateRequest(BaseModel):
    pair: str
    market_type: str
    notification_channel: str
    risk_pct: float | None = Field(default=None, gt=0, le=100)
    api_key: str = Field(min_length=1)
    api_secret: str = Field(min_length=1)

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


class AccountUpdateRequest(BaseModel):
    risk_pct: float | None = Field(default=None, gt=0, le=100)
    is_active: bool | None = None


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


class CheckoutSessionOut(BaseModel):
    checkout_url: str


class BillingPortalOut(BaseModel):
    portal_url: str
