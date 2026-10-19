"""ccxt client construction and a runtime permission safety check.

Uses `ccxt.async_support` (not the sync `ccxt` module) so the shared
scheduler loop can hold many customers' exchange connections concurrently
in one process without threads - see ARCHITECTURE.md for why that's the
recommended deployment shape here.
"""
from __future__ import annotations

import hashlib
import time

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


class UnsupportedAccountMode(RuntimeError):
    """The Binance account is in a configuration whose order and protective
    -stop semantics this bot does not (and must not guess to) handle:
    Hedge Mode, Multi-Assets Mode, Portfolio Margin, or a mode that could
    not be read at all. A new live entry is refused; nothing is ever
    switched on the customer's behalf."""


class UnsupportedSymbol(RuntimeError):
    """The pair is not available as an active market of the right kind on
    this exchange/environment, so no order is placed for it."""


def quote_currency(pair: str) -> str:
    return pair.split("/", 1)[1]


def exchange_symbol(pair: str, market_type: str) -> str:
    """ccxt's unified symbol for `pair` on `market_type`.

    The platform stores pairs as spot-style "BTC/USDT". On the Spot client
    that IS the ccxt symbol. On the USD-M client ccxt's symbol for the
    perpetual is "BTC/USDT:USDT" (settle currency after the colon); a bare
    "BTC/USDT" is neither a ccxt symbol nor a Binance market id there
    (BTCUSDT), so passing it through raises BadSymbol - this is the
    normalization the Futures path always needed. Pure and deterministic;
    the live path additionally verifies the result against loaded markets
    (`verify_market`)."""
    if market_type == "futures":
        return f"{pair}:{quote_currency(pair)}"
    return pair


def verify_market(client: ccxt_async.Exchange, symbol: str, *, market_type: str) -> dict:
    """The loaded market for `symbol`, or UnsupportedSymbol. Requires
    `client.load_markets()` to have run. For Futures it must be an active
    LINEAR perpetual; for Spot an active spot market."""
    markets = getattr(client, "markets", None) or {}
    market = markets.get(symbol)
    if not isinstance(market, dict):
        raise UnsupportedSymbol(f"{symbol} is not a market on this exchange")
    if market.get("active") is False:
        raise UnsupportedSymbol(f"{symbol} is not currently active")
    if market_type == "futures":
        if not (market.get("swap") is True and market.get("linear") is True and market.get("contract") is True):
            raise UnsupportedSymbol(f"{symbol} is not a linear USD-M perpetual")
    else:
        if market.get("spot") is not True:
            raise UnsupportedSymbol(f"{symbol} is not a spot market")
    return market


def _as_bool(value) -> bool | None:
    if isinstance(value, bool):
        return value
    if isinstance(value, str) and value.strip().lower() in ("true", "false"):
        return value.strip().lower() == "true"
    return None


# Successful mode verifications, keyed by a digest of the API key (never the
# key itself), so MANAGING an open position (every 5 min, plus the 15 s
# recovery loop) doesn't spend Binance request weight re-reading two slow
# account settings each time. A NEW entry never uses it (max_age_seconds=0),
# and a failed verification is never cached.
_MODE_VERIFIED_AT: dict[str, float] = {}


def _mode_cache_key(client) -> str | None:
    api_key = getattr(client, "apiKey", None)
    if not api_key:
        return None
    return hashlib.sha256(str(api_key).encode("utf-8")).hexdigest()


async def verify_futures_account_mode(client: ccxt_async.Exchange, symbol: str, *, max_age_seconds: float = 0) -> None:
    """Fail closed unless the account is positively confirmed to be in the
    one configuration this bot supports: USD-M, ONE-WAY position mode,
    single-asset margin, NOT Portfolio Margin.

    "Positively confirmed" matters: every read below must succeed and
    return the safe value. A failed read, a missing field or an
    unexpected shape raises UnsupportedAccountMode, never a guess. This is
    read-only; no mode is ever changed.

    Known limit (see ARCHITECTURE.md): Portfolio Margin has no dedicated
    "is this PM" read on the classic Futures API. PM accounts cannot use
    the classic endpoints above (they must use `papi`), so a PM account is
    expected to fail the reads here and be refused; the extra SAPI probe
    only adds a positive detection where the key may call it. Whether
    Binance behaves exactly that way must be confirmed by the Testnet/
    Mainnet read-only validation before customers trade."""
    key = _mode_cache_key(client)
    if key is not None and max_age_seconds > 0:
        verified_at = _MODE_VERIFIED_AT.get(key)
        if verified_at is not None and time.monotonic() - verified_at < max_age_seconds:
            return
    try:
        mode = await client.fetch_position_mode(symbol)
    except Exception as exc:
        raise UnsupportedAccountMode("could not read the Binance Futures position mode") from exc
    hedged = mode.get("hedged") if isinstance(mode, dict) else None
    if hedged is not False:
        raise UnsupportedAccountMode("Binance Futures Hedge Mode is not supported" if hedged else "position mode unreadable")

    try:
        multi = await client.fapiPrivateGetMultiAssetsMargin()
    except Exception as exc:
        raise UnsupportedAccountMode("could not read the Binance Futures Multi-Assets Mode setting") from exc
    multi_value = _as_bool(multi.get("multiAssetsMargin")) if isinstance(multi, dict) else None
    if multi_value is not False:
        raise UnsupportedAccountMode(
            "Binance Futures Multi-Assets Mode is not supported" if multi_value else "multi-assets mode unreadable"
        )

    options = getattr(client, "options", {}) or {}
    if options.get("portfolioMargin") or options.get("papi"):
        raise UnsupportedAccountMode("Portfolio Margin routing is not supported")
    probe = getattr(client, "sapiGetPortfolioAccount", None)
    if probe is not None:
        try:
            result = await probe()
        except Exception:
            result = None
        if isinstance(result, dict) and result:
            raise UnsupportedAccountMode("Binance Portfolio Margin accounts are not supported")
    if key is not None:
        _MODE_VERIFIED_AT[key] = time.monotonic()


def safe_error_message(exc: Exception) -> str:
    """Turns any exception from a trading cycle into a short, generic,
    customer-facing message. Deliberately never includes `str(exc)` -
    ccxt/exchange error text can echo back request details (key
    fragments, raw HTTP bodies, internal URLs) that have no business
    reaching a customer's dashboard. Only the category is shown; the
    real exception and its traceback still go to the server log via
    `logger.exception` in scheduler.py, unaffected by this function.
    """
    if isinstance(exc, UnsupportedAccountMode):
        return (
            "Your Binance Futures account is set to a mode this bot does not support (it needs standard "
            "One-way Mode with single-asset margin, not Hedge Mode, Multi-Assets Mode or Portfolio Margin). "
            "No new trade was opened."
        )
    if isinstance(exc, UnsupportedSymbol):
        return "This pair is not currently available to trade on Binance for your account type. No new trade was opened."
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
