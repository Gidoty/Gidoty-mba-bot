"""Central configuration, loaded from environment variables.

Every tunable used by strategy/risk/scheduler code lives here so behaviour
can be changed per-deployment without touching code. Customer-specific
settings (pair, market type, risk %) live in the database instead -
this module only holds defaults and operational knobs.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field

from dotenv import load_dotenv

load_dotenv()


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


SUPPORTED_PAIRS = ("BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT")
# Not tradeable yet - CORE isn't listed on Binance. Shown in the
# frontend's pair selector as a disabled "coming soon" option; never
# added here, since accepting it would let an account get created for a
# pair the exchange integration can't actually trade.
UPCOMING_PAIRS = ("CORE/USDT",)
SUPPORTED_MARKET_TYPES = ("spot", "futures")
SUPPORTED_NOTIFICATION_CHANNELS = ("telegram", "whatsapp")


@dataclass(frozen=True)
class Settings:
    database_url: str
    encryption_key: str | None = field(repr=False)

    poll_interval_seconds: int
    timeframe: str
    ohlcv_limit: int

    trial_days: int

    ema_fast_period: int
    ema_slow_period: int
    atr_period: int
    atr_stop_multiplier: float
    # ATR must be at least this fraction of its own rolling average for a
    # crossover signal to be traded; below it the market is considered flat
    # / range-bound and the signal is skipped.
    atr_flat_threshold: float
    atr_rolling_window: int

    risk_pct_default: float
    risk_pct_max: float

    # Caps notional exposure (quantity * entry_price) at
    # `effective_capital * max_notional_multiplier` for every live entry
    # - see risk.apply_exposure_ceiling for the exact formula and why it
    # exists (a tight ATR stop alone has no limit on how large the
    # risk-% formula can size a position). 1.0 is the natural, no-op
    # value for Spot (you can never spend more than you have there).
    # Deliberately independent of whatever leverage is actually
    # configured on the exchange account - see risk.apply_margin_ceiling
    # for the separate check that does use the real, read-only leverage
    # value. Conservative default: well under what even 1x-leveraged
    # Spot would ever hit, but gives Futures some genuine room before
    # the ceiling binds - an operator decision, not a mathematical one,
    # so it is intentionally configurable rather than hardcoded.
    max_notional_multiplier: float

    # Shared platform-level Binance testnet keys used for every account's
    # paper-trading trial - never exposed to customers. Split by market
    # type because Binance's spot testnet (testnet.binance.vision) and
    # futures testnet (testnet.binancefuture.com) are separate systems
    # with separate key registrations; a key issued on one is not valid
    # on the other. A customer's own key (ApiCredential) is only ever
    # used once an account reaches LIVE - see models.py/onboarding.py.
    testnet_spot_api_key: str | None = field(repr=False)
    testnet_spot_api_secret: str | None = field(repr=False)
    testnet_futures_api_key: str | None = field(repr=False)
    testnet_futures_api_secret: str | None = field(repr=False)
    paper_starting_balance: float

    telegram_bot_token: str | None = field(repr=False)
    telegram_bot_username: str | None
    whatsapp_provider_token: str | None = field(repr=False)

    # --- web API (api/) ---
    jwt_secret: str | None = field(repr=False)
    jwt_algorithm: str
    jwt_expires_minutes: int
    frontend_origin: str
    # False only for local HTTP development/tests - a deployed instance
    # must keep this True so the session cookie is never sent over plain
    # HTTP.
    cookie_secure: bool

    # Paystack: one secret key both signs API calls and verifies webhook
    # signatures (HMAC-SHA512 over the raw body) - no separate webhook
    # secret like Stripe has. `paystack_amount` is in the currency's
    # lowest unit (kobo for NGN), matching Paystack's own API.
    paystack_secret_key: str | None = field(repr=False)
    paystack_amount: int
    paystack_currency: str

    # --- exchange-side protective stop / lifecycle (venue.py, lifecycle.py) ---
    # Attempts to place AND verify the resting protective stop after a
    # confirmed entry fill, only for errors that are plausibly transient.
    # A deterministic rejection (see lifecycle._is_transient) never retries.
    protection_max_attempts: int = 3
    protection_retry_delay_seconds: float = 2.0
    # How long to poll an entry order whose response was lost/incomplete
    # before leaving it for the reconciler (never re-sent either way).
    entry_resolve_attempts: int = 5
    entry_resolve_delay_seconds: float = 1.0
    # An entry that has no exchange order at all (client id unknown to
    # Binance) is only voided once it is at least this old - a request
    # that timed out client-side may still be in flight at Binance.
    entry_unknown_void_after_seconds: int = 180
    # Spot STOP_LOSS_LIMIT only: the limit sits this % below the trigger
    # so a fast move still has room to fill (see venue.SpotVenue).
    spot_stop_limit_buffer_pct: float = 0.5
    # Software backstop: a resting stop that the price has run past by
    # more than this % without the exchange having fired it is treated as
    # failed, and the trade is closed through the normal exit lease.
    protection_software_grace_pct: float = 0.5
    # Fast reconciliation loop for trades in an unhealthy state
    # (scheduler.run_recovery_forever).
    recovery_interval_seconds: int = 15
    # Spot STOP_LOSS_LIMIT only: how often the price of every open
    # stop-limit-protected position is compared with its trigger (one cheap
    # public ticker call per distinct symbol per pass, shared by all
    # customers); only a position whose price has reached its trigger gets
    # authenticated exchange calls. Floor is the recovery loop's own tick.
    stop_limit_monitor_interval_seconds: int = 15
    # Single-instance guard (main.py); a second scheduler process refuses
    # to start while this file is locked.
    lock_file: str = "./mba_bot.lock"


def load_settings() -> Settings:
    return Settings(
        database_url=os.environ.get("DATABASE_URL", "sqlite:///./mba_bot.db"),
        encryption_key=os.environ.get("ENCRYPTION_KEY"),
        poll_interval_seconds=_env_int("POLL_INTERVAL_SECONDS", 300),
        timeframe=os.environ.get("TIMEFRAME", "1h"),
        ohlcv_limit=_env_int("OHLCV_LIMIT", 200),
        trial_days=_env_int("TRIAL_DAYS", 7),
        ema_fast_period=_env_int("EMA_FAST_PERIOD", 20),
        ema_slow_period=_env_int("EMA_SLOW_PERIOD", 50),
        atr_period=_env_int("ATR_PERIOD", 14),
        atr_stop_multiplier=_env_float("ATR_STOP_MULTIPLIER", 2.0),
        atr_flat_threshold=_env_float("ATR_FLAT_THRESHOLD", 0.75),
        atr_rolling_window=_env_int("ATR_ROLLING_WINDOW", 50),
        risk_pct_default=_env_float("RISK_PCT_DEFAULT", 1.0),
        risk_pct_max=_env_float("RISK_PCT_MAX", 2.0),
        max_notional_multiplier=_env_float("MAX_NOTIONAL_MULTIPLIER", 3.0),
        # TESTNET_API_KEY/SECRET are the legacy, pre-split var names -
        # kept as a fallback for the spot pair only, so an existing
        # deployment's working spot paper trading doesn't go dark the
        # moment this ships. Futures has no such legacy name to fall
        # back to: it was silently sharing the spot key before this
        # change (the actual bug being fixed here), so it now fails
        # safe (no usable credentials) until TESTNET_FUTURES_API_KEY/
        # SECRET is set explicitly, rather than keep guessing.
        testnet_spot_api_key=os.environ.get("TESTNET_SPOT_API_KEY") or os.environ.get("TESTNET_API_KEY"),
        testnet_spot_api_secret=os.environ.get("TESTNET_SPOT_API_SECRET") or os.environ.get("TESTNET_API_SECRET"),
        testnet_futures_api_key=os.environ.get("TESTNET_FUTURES_API_KEY"),
        testnet_futures_api_secret=os.environ.get("TESTNET_FUTURES_API_SECRET"),
        paper_starting_balance=_env_float("PAPER_STARTING_BALANCE", 10_000.0),
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN"),
        telegram_bot_username=os.environ.get("TELEGRAM_BOT_USERNAME"),
        whatsapp_provider_token=os.environ.get("WHATSAPP_PROVIDER_TOKEN"),
        jwt_secret=os.environ.get("JWT_SECRET"),
        jwt_algorithm=os.environ.get("JWT_ALGORITHM", "HS256"),
        jwt_expires_minutes=_env_int("JWT_EXPIRES_MINUTES", 60 * 24 * 7),
        frontend_origin=os.environ.get("FRONTEND_ORIGIN", "http://localhost:5173"),
        cookie_secure=_env_bool("COOKIE_SECURE", True),
        paystack_secret_key=os.environ.get("PAYSTACK_SECRET_KEY"),
        paystack_amount=_env_int("PAYSTACK_AMOUNT", 0),
        paystack_currency=os.environ.get("PAYSTACK_CURRENCY", "NGN"),
        protection_max_attempts=_env_int("PROTECTION_MAX_ATTEMPTS", 3),
        protection_retry_delay_seconds=_env_float("PROTECTION_RETRY_DELAY_SECONDS", 2.0),
        entry_resolve_attempts=_env_int("ENTRY_RESOLVE_ATTEMPTS", 5),
        entry_resolve_delay_seconds=_env_float("ENTRY_RESOLVE_DELAY_SECONDS", 1.0),
        entry_unknown_void_after_seconds=_env_int("ENTRY_UNKNOWN_VOID_AFTER_SECONDS", 180),
        spot_stop_limit_buffer_pct=_env_float("SPOT_STOP_LIMIT_BUFFER_PCT", 0.5),
        protection_software_grace_pct=_env_float("PROTECTION_SOFTWARE_GRACE_PCT", 0.5),
        recovery_interval_seconds=_env_int("RECOVERY_INTERVAL_SECONDS", 15),
        stop_limit_monitor_interval_seconds=_env_int("STOP_LIMIT_MONITOR_INTERVAL_SECONDS", 15),
        lock_file=os.environ.get("MBA_BOT_LOCK_FILE", "./mba_bot.lock"),
    )
