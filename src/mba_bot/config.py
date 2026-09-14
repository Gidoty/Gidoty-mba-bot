"""Central configuration, loaded from environment variables.

Every tunable used by strategy/risk/scheduler code lives here so behaviour
can be changed per-deployment without touching code. Customer-specific
settings (pair, market type, risk %) live in the database instead -
this module only holds defaults and operational knobs.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(name, default))


def _env_int(name: str, default: int) -> int:
    return int(os.environ.get(name, default))


SUPPORTED_PAIRS = ("BTC/USDT", "ETH/USDT", "BNB/USDT", "XRP/USDT")
SUPPORTED_MARKET_TYPES = ("spot", "futures")
SUPPORTED_NOTIFICATION_CHANNELS = ("telegram", "whatsapp")


@dataclass(frozen=True)
class Settings:
    database_url: str
    encryption_key: str | None

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

    # Shared platform-level Binance testnet key used for every account's
    # paper-trading trial. Each customer's own mainnet key (ApiCredential)
    # is only used once they reach live trading - see models.py.
    testnet_api_key: str | None
    testnet_api_secret: str | None
    paper_starting_balance: float

    telegram_bot_token: str | None
    whatsapp_provider_token: str | None


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
        testnet_api_key=os.environ.get("TESTNET_API_KEY"),
        testnet_api_secret=os.environ.get("TESTNET_API_SECRET"),
        paper_starting_balance=_env_float("PAPER_STARTING_BALANCE", 10_000.0),
        telegram_bot_token=os.environ.get("TELEGRAM_BOT_TOKEN"),
        whatsapp_provider_token=os.environ.get("WHATSAPP_PROVIDER_TOKEN"),
    )
