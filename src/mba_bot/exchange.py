"""ccxt client construction and a runtime permission safety check.

Uses `ccxt.async_support` (not the sync `ccxt` module) so the shared
scheduler loop can hold many customers' exchange connections concurrently
in one process without threads - see ARCHITECTURE.md for why that's the
recommended deployment shape here.
"""
from __future__ import annotations

import ccxt.async_support as ccxt_async

_EXCHANGE_CLASSES = {
    ("binance", "spot"): ccxt_async.binance,
    ("binance", "futures"): ccxt_async.binanceusdm,
}


class UnsupportedExchange(ValueError):
    pass


class WithdrawalPermissionDetected(RuntimeError):
    """Stored keys must be trade-only. Raised if a runtime check finds a
    key that can withdraw - trading is refused for that account until the
    key is replaced, even if trade-only was intended at signup."""


def build_exchange_client(
    *, exchange: str, market_type: str, api_key: str, api_secret: str, sandbox: bool
) -> ccxt_async.Exchange:
    exchange_cls = _EXCHANGE_CLASSES.get((exchange, market_type))
    if exchange_cls is None:
        raise UnsupportedExchange(f"no ccxt client configured for exchange={exchange!r} market_type={market_type!r}")

    client = exchange_cls({"apiKey": api_key, "secret": api_secret, "enableRateLimit": True})
    if sandbox:
        client.set_sandbox_mode(True)
    return client


async def assert_trade_only(client: ccxt_async.Exchange) -> None:
    """Best-effort defense in depth: confirm the key can't withdraw before
    trading with it. Not every exchange/market exposes this the same way
    in `fetch_balance()['info']`, so callers should log-and-continue on
    unexpected shapes rather than treat that as fatal - only an explicit
    `canWithdraw: true` blocks trading.
    """
    balance = await client.fetch_balance()
    info = balance.get("info", {}) if isinstance(balance, dict) else {}
    if isinstance(info, dict) and info.get("canWithdraw") is True:
        raise WithdrawalPermissionDetected(
            "stored API key has withdrawal permission enabled - revoke it and issue a trade-only key"
        )


def quote_currency(pair: str) -> str:
    return pair.split("/", 1)[1]


def safe_error_message(exc: Exception) -> str:
    """Turns any exception from a trading cycle into a short, generic,
    customer-facing message. Deliberately never includes `str(exc)` -
    ccxt/exchange error text can echo back request details (key
    fragments, raw HTTP bodies, internal URLs) that have no business
    reaching a customer's dashboard. Only the category is shown; the
    real exception and its traceback still go to the server log via
    `logger.exception` in scheduler.py, unaffected by this function.
    """
    if isinstance(exc, WithdrawalPermissionDetected):
        return (
            "Your Binance API key has withdrawal permission enabled. For your safety, trading is "
            "paused on this account until you replace it with a trade-only key."
        )
    if isinstance(exc, ccxt_async.AuthenticationError):
        return "Binance rejected this account's API key or secret. Please check it in your Binance account."
    if isinstance(exc, ccxt_async.InsufficientFunds):
        return "Your Binance balance is too low for the bot's current position size."
    if isinstance(exc, (ccxt_async.NetworkError, ccxt_async.ExchangeNotAvailable, ccxt_async.RequestTimeout)):
        return "Temporary connection issue reaching Binance. The bot will try again on its next check."
    if isinstance(exc, ccxt_async.ExchangeError):
        return "Binance rejected a request from the bot. Please check your account settings on Binance."
    return "An unexpected error occurred while checking the market for this account."


async def fetch_symbol_leverage(client: ccxt_async.Exchange, pair: str, *, side: str) -> float | None:
    """Read-only. Returns the account's currently configured leverage for
    `pair`/`side` ("long" or "short"), or None if it can't be determined.

    This module never calls `set_leverage` - reading ccxt's own source
    for `binanceusdm.set_leverage` shows it is a mutating call with real,
    account-wide, cross-position side effects (it can change margin mode
    and liquidation prices for existing positions), which a trading bot
    must never do silently on a customer's behalf. `fetch_leverage`/
    `fetch_leverages`, by contrast, are plain read-only GETs (they back
    onto Binance's symbol-config/account-info endpoints) - safe to call
    every cycle purely to learn what leverage is already configured.

    A None return means the caller's margin ceiling (risk.
    apply_margin_ceiling) is simply skipped for this entry - never
    treated as "assume some leverage" or "assume no leverage". The
    independent notional ceiling (risk.apply_exposure_ceiling) still
    applies regardless of this result, so this is never the only safety
    net standing between a tight stop and an oversized position.
    """
    try:
        leverage = await client.fetch_leverage(pair)
    except Exception:
        return None
    if not isinstance(leverage, dict):
        return None
    value = leverage.get("longLeverage") if side == "long" else leverage.get("shortLeverage")
    if value is None:
        return None
    try:
        value = float(value)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


async def fetch_available_quote_balance(client: ccxt_async.Exchange, pair: str) -> float:
    balance = await client.fetch_balance()
    quote = quote_currency(pair)
    entry = balance.get(quote)
    if isinstance(entry, dict) and entry.get("free") is not None:
        return float(entry["free"])
    free_map = balance.get("free", {})
    if isinstance(free_map, dict):
        return float(free_map.get(quote, 0.0) or 0.0)
    return 0.0
