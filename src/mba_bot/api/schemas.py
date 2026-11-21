"""Pydantic request/response models for the public API.

Pair/market-type/notification-channel membership is validated here
against `config.SUPPORTED_*` so there's one list of allowed values, not
one in the schema and another in the strategy/trader code.
"""
from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, EmailStr, Field, StrictInt, field_validator, model_serializer

from ..config import ALL_KNOWN_PAIRS, SUPPORTED_MARKET_TYPES, SUPPORTED_NOTIFICATION_CHANNELS

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
    # Only needed (and then required true) for a customer who already has live trading enabled and has not accepted the current Risk Disclosure; the route checks that, not this model.
    accept_risk_disclosure: bool = False

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
    # True only for a customer who already has live trading enabled and has not accepted the CURRENT Risk Disclosure: the consent screen then shows a third box.
    risk_acceptance_needed: bool = False

    model_config = {"from_attributes": True}


class AccountCreateRequest(BaseModel):
    pair: str
    market_type: str
    notification_channel: str
    risk_pct: float | None = Field(default=None, gt=0, le=100)
    # Recorded acknowledgments for a risk setting above 2 percent / from 10 percent (the server refuses the setting without them; see risk_limits.py).
    acknowledge_high_risk: bool = False
    acknowledge_very_high_risk: bool = False
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
        return _validate_choice(v, ALL_KNOWN_PAIRS, "pair")

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


class DemoCredentialsRequest(BaseModel):
    """Keys created on Binance's DEMO system (Futures Demo Trading for a Futures account, Spot Testnet for a Spot account). Never a live key."""

    api_key: str = Field(min_length=1)
    api_secret: str = Field(min_length=1)


class DemoModeRequest(BaseModel):
    enabled: bool
    confirm: bool

    @field_validator("confirm")
    @classmethod
    def _must_confirm(cls, v: bool) -> bool:
        if not v:
            raise ValueError("confirm must be true - this endpoint only does something when you confirm")
        return v


PAIR_SELECTION_MODES = ("manual", "auto")


class AccountUpdateRequest(BaseModel):
    risk_pct: float | None = Field(default=None, gt=0, le=100)
    acknowledge_high_risk: bool = False
    acknowledge_very_high_risk: bool = False
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
    # The customer's daily loss limit in USDT (docs/safety/account_daily_loss.md). Omitted = unchanged. Lower applies at once, higher starts at the next midnight Lagos time.
    daily_loss_limit_usd: float | None = Field(default=None, gt=0)
    # How many live trades the bot may hold at once, 1 to 3 (only while LIVE_MULTI_POSITION is on; otherwise refused). Omitted = unchanged.
    live_slots: int | None = Field(default=None, ge=1, le=3)
    # The EXTRA pairs for a live account holding more than one trade (the bot trades `pair` first, then these). At most 2, no duplicates, [] clears. Only while LIVE_MULTI_POSITION is on; the router checks the rest.
    pair_list: list[str] | None = Field(default=None, max_length=2)
    # Customer leverage setting (Futures; Spot ignores it): a whole number from 1 to 5, never above the global hard limit. The router checks the global limit.
    # Strict int: 3.0, "3" and true are rejected.
    max_leverage: StrictInt | None = Field(default=None, ge=1, le=5)

    @field_validator("pair")
    @classmethod
    def _valid_pair(cls, v: str | None) -> str | None:
        return v if v is None else _validate_choice(v, ALL_KNOWN_PAIRS, "pair")

    @field_validator("market_type")
    @classmethod
    def _valid_market_type(cls, v: str | None) -> str | None:
        return v if v is None else _validate_choice(v, SUPPORTED_MARKET_TYPES, "market_type")

    @field_validator("pair_selection_mode")
    @classmethod
    def _valid_pair_selection_mode(cls, v: str | None) -> str | None:
        return v if v is None else _validate_choice(v, PAIR_SELECTION_MODES, "pair_selection_mode")

    @field_validator("pair_list")
    @classmethod
    def _valid_pair_list(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return None
        cleaned = [_validate_choice(p.strip().upper() if isinstance(p, str) else p, ALL_KNOWN_PAIRS, "pair") for p in v]
        if len(set(cleaned)) != len(cleaned):
            raise ValueError("each pair can be chosen once")
        return cleaned


class AdviceRequest(BaseModel):
    """What the customer is about to save, for the setup advisor. Deliberately loose: a number the account API would refuse is answered with a 'not possible' note, not an error here."""

    risk_pct: float | None = None
    allocated_capital: float | None = None
    daily_loss_limit_usd: float | None = None
    live_slots: int | None = None
    max_leverage: int | None = None
    pair: str | None = None
    market_type: str | None = None
    pair_selection_mode: str | None = None
    acknowledge_high_risk: bool = False
    acknowledge_very_high_risk: bool = False


class AdviceNote(BaseModel):
    id: str
    severity: str  # "not_possible" | "warning" | "tip"
    text: str
    action: str | None = None
    tab: str  # "trading" | "risk"


class AdviceOut(BaseModel):
    notes: list[AdviceNote]


class BinanceSettingsOut(BaseModel):
    """What Binance currently has for the account's pair, read-only. `text` is always one of a few fixed customer-safe strings (see leverage.py); the two values are numbers and words only."""

    status: str  # "ok" | "not_applicable" | "unavailable"
    pair: str | None = None
    leverage: float | None = None
    margin_mode: str | None = None  # "isolated" | "cross" | None
    text: str


class TotalLossResetRequest(BaseModel):
    """The customer's confirmation that new trades may start again after a total loss stop (or that the reference value should be taken afresh)."""

    confirm: bool

    @field_validator("confirm")
    @classmethod
    def _must_confirm(cls, v: bool) -> bool:
        if not v:
            raise ValueError("confirm must be true - this endpoint only does something when you confirm")
        return v


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
    # The Risk Disclosure version the screen showed. Optional for older clients; when present it must be the current one, so an acceptance can never be recorded against text the customer did not see.
    risk_disclosure_version: str | None = Field(default=None, max_length=20)

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
    # Set to "gross_before_fees" only while PNL_LABELS is on: profit figures are before exchange fees and funding.
    pnl_basis: str | None = None
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
    # The customer's leverage number as the bot reads it (3 when never set, never above the limit) and the highest number they may choose.
    max_leverage: int = 3
    max_leverage_limit: int = 5
    # The highest risk per trade a customer may choose right now (RISK_PCT_MAX, code ceiling 40) and the total loss stop state (docs/safety/risk_per_trade.md).
    risk_pct_limit: float = 2.0
    total_loss_stop_enabled: bool = False
    total_loss_stop_active: bool = False
    total_loss_stop_threshold_pct: float = 10.0
    total_loss_reference_equity: float | None = None
    # The customer's daily loss limit (docs/safety/account_daily_loss.md). `daily_loss_limit_usd` is the amount in force today; a raise is pending until `daily_loss_limit_pending_from`
    # (a day, YYYY-MM-DD). `daily_loss_limit_is_default` is true while the amount is the one-time default the service gave. The percent is of `account_value_reference` (the total stop's
    # reference value, else the last stored value, else the trading capital). The amount must be below `daily_loss_limit_max_usd` (the dollar level of the automatic total stop).
    daily_loss_limit_usd: float | None = None
    daily_loss_limit_pending_usd: float | None = None
    daily_loss_limit_pending_from: str | None = None
    daily_loss_limit_is_default: bool = False
    daily_loss_limit_required: bool = True
    daily_loss_limit_enforced: bool = False
    daily_loss_limit_pct: float | None = None
    daily_loss_limit_suggested_usd: float | None = None
    daily_loss_limit_max_usd: float | None = None
    account_value_reference: float | None = None
    one_trade_can_exceed_limit: bool = False
    # SMALL_CAPITAL_PAIRS: set while the bot has recently skipped the account's own pair because its capital cannot meet the pair's smallest order (a plain text and the capital needed).
    pair_skip_message: str | None = None
    pair_skip_min_capital: float | None = None
    # DEMO mode (DEMO_RUNNER): `demo_available` says the service offers it, `demo_has_keys` that demo keys are on file, `demo_enabled_at` that the customer switched it on (then `mode` is "demo").
    reports_available: bool = False
    live_slots: int = 1
    live_multi_available: bool = False
    live_pair_count: int = 1
    # Only while LIVE_MULTI_POSITION is on (otherwise None, as today): the extra pairs the customer chose, and the read-only open-risk cap in percent of the trading capital.
    live_pair_list: list[str] | None = None
    live_open_risk_cap_pct: float | None = None
    demo_available: bool = False
    demo_has_keys: bool = False
    demo_enabled_at: datetime | None = None

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
    # True while the stored key is known to have withdrawal permission: no new
    # trades; any open position is still managed for risk reduction only.
    credential_blocked: bool = False
    credential_block_message: str | None = None

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
    # Additive (multi-trade foundation). Both can be null on old rows: a null pair means the account's pair, a null
    # strategy_id means ema_cross_atr_v1.
    pair: str | None = None
    strategy_id: str | None = None
    # True for a DEMO trade (the demo runner). Live and paper screens exclude these.
    is_demo: bool = False

    model_config = {"from_attributes": True}


class TradesSummaryOut(BaseModel):
    # Same meaning as AccountOut.pnl_basis. `net_pnl` keeps its name for compatibility but is GROSS (before fees and funding).
    pnl_basis: str | None = None
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


class BillingCardOptionOut(BaseModel):
    available: bool  # Paystack is configured (key and plan): the checkout route would not answer 503
    can_start: bool  # the checkout route would accept this customer now (no subscription, grace, or expired)
    amount_minor: int  # PAYSTACK_PLAN_AMOUNT, in the smallest currency unit
    currency: str


class BillingCryptoOptionOut(BaseModel):
    available: bool  # NOWPAYMENTS_ENABLED and configured
    can_start: bool
    price_usd: str  # NOWPAYMENTS_PRICE_USD as a decimal string with two places


class BillingOptionsOut(BaseModel):
    """What the Activate screen and the subscription card need, from the server settings. Fixed fields only: no payment reference, no provider link, no key, no e-mail."""

    state: str  # none | active | grace | cancelled | expired | legacy_lifetime
    provider: str | None  # paystack | nowpayments | manual, from the subscription row, or None
    period_end: datetime | None
    grace_end: datetime | None
    grace_days: int
    card: BillingCardOptionOut
    crypto: BillingCryptoOptionOut


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


class OpenTradeOut(BaseModel):
    """One non-terminal trade, from the database only (no price lookup). For GET /api/account/positions."""

    id: str
    pair: str | None = None
    side: str
    status: str
    is_paper: bool
    strategy_id: str  # a null column is shown as ema_cross_atr_v1
    entry_price: float
    quantity: float
    stop_loss_price: float
    opened_at: datetime
    protected: bool | None = None


class OpenTradesOut(BaseModel):
    open_count: int
    trades: list[OpenTradeOut]


class CredentialTestOut(BaseModel):
    ok: bool
    message: str


class PairOut(BaseModel):
    pair: str
    available: bool
    # Customer-facing reason when `available` is False; None otherwise.
    reason: str | None = None
    # SMALL_CAPITAL_PAIRS: the trading capital this pair's smallest order needs at the customer's leverage cap (set only when the pair is unavailable for being too small).
    min_capital: float | None = None

    @model_serializer(mode="wrap")
    def _omit_unset_min_capital(self, handler):
        """With SMALL_CAPITAL_PAIRS off the answer is byte for byte what it always was: the new key appears only when it has a value."""
        data = handler(self)
        if data.get("min_capital") is None:
            data.pop("min_capital", None)
        return data


class PairsOut(BaseModel):
    market_type: str
    pairs: list[PairOut]
