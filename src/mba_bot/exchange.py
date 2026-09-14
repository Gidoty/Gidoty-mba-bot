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
