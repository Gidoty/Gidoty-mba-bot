"""Foreign-order alarm (docs/safety/foreign_orders.md): tell the customer when their Binance account holds an open order or a position that the bot did not open. It NEVER touches it.

Read-only. Off by default (FOREIGN_ORDER_ALARM). For a LIVE account that has an exchange client, at most once every FOREIGN_ORDER_CHECK_MINUTES, the account's own open orders (per symbol of the pairs
the service offers for its market, regular and, on Futures, conditional) and, on Futures, its open positions are read. A finding is:

  * an open order whose client order id does not carry the bot's prefix (`venue.CLIENT_ID_PREFIX`), on any of those symbols; or
  * a Futures position on a symbol for which the bot has no open trade.

Spot balances are NOT checked (coins held for other reasons are normal). The alert is sent once per distinct set of findings per 24 hours, with a fixed customer-safe text, one decision-log row and one
journal line. The alarm blocks nothing, cancels nothing, closes nothing and places nothing: the only exchange calls are the reads above."""
from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass

from . import venue as venue_mod

MODE_OFF, MODE_ALERT = "off", "alert"
R_FOREIGN = "foreign_order_or_position"

MESSAGE = (
    "MBA Bot noticed {what} on your Binance account that it did not open{where}. The bot has not touched {them} and will not. "
    "If you did not place {them} yourself, check your Binance account and your API key."
)


def parse_mode(raw: str | None) -> str:
    """off (default) or alert. 1, true, yes, on and alert mean alert; anything else, unset or empty is off."""
    return MODE_ALERT if (raw or "").strip().lower() in ("1", "true", "yes", "on", "alert") else MODE_OFF


@dataclass(frozen=True)
class Finding:
    kind: str  # "order" | "position"
    symbol: str
    key: str  # what distinguishes it for the alert de-duplication (an order id, or a position side)


def _symbol_key(symbol) -> str:
    """BTC/USDT:USDT and BTC/USDT are the same pair for comparing with the bot's own trades."""
    return str(symbol or "").split(":")[0].upper()


def find_foreign(orders, positions, bot_pairs) -> list[Finding]:
    """Pure. `orders` and `positions` are ccxt-shaped lists, `bot_pairs` the pairs (BTC/USDT) with a non-terminal trade the bot opened. Anything that cannot be read as a number or a string is
    skipped as not a finding rather than guessed (the alarm must not invent an alert)."""
    owned_pairs = {_symbol_key(p) for p in bot_pairs}
    out: list[Finding] = []
    for o in orders or []:
        if not isinstance(o, dict):
            continue
        client_id = o.get("clientOrderId")
        if venue_mod._bot_owned(None if client_id is None else str(client_id)):
            continue
        out.append(Finding("order", _symbol_key(o.get("symbol")), str(o.get("id") or client_id or "?")))
    for p in positions or []:
        if not isinstance(p, dict):
            continue
        raw = p.get("contracts")
        if raw is None and isinstance(p.get("info"), dict):
            raw = p["info"].get("positionAmt")
        try:
            amount = float(raw)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(amount) or amount == 0:
            continue
        symbol = _symbol_key(p.get("symbol"))
        if symbol in owned_pairs:
            continue
        out.append(Finding("position", symbol, str(p.get("side") or ("long" if amount > 0 else "short"))))
    return sorted(set(out), key=lambda f: (f.kind, f.symbol, f.key))


def fingerprint(findings: list[Finding]) -> str:
    return hashlib.sha256("|".join(f"{f.kind}:{f.symbol}:{f.key}" for f in findings).encode()).hexdigest()[:16]


def message(findings: list[Finding]) -> str:
    n = len(findings)
    symbols = sorted({f.symbol for f in findings if f.symbol})
    where = f" (on {', '.join(symbols[:3])}{' and others' if len(symbols) > 3 else ''})" if symbols else ""
    return MESSAGE.format(what="an open order or position" if n == 1 else f"{n} open orders or positions", where=where, them="it" if n == 1 else "them")


async def read_account_state(client, market_type: str, symbols: list[str]) -> tuple[list, list, int]:
    """(open orders, open positions, symbols that could not be read). Only fetch_* calls. A symbol whose read fails is skipped and counted: a partial look is still a look, and a failed read is
    never turned into an alert."""
    orders: list = []
    positions: list = []
    failed = 0
    for symbol in symbols:
        try:
            orders.extend(await client.fetch_open_orders(symbol) or [])
            if market_type == "futures":
                orders.extend(await client.fetch_open_orders(symbol, None, None, {"trigger": True}) or [])
        except Exception:  # noqa: BLE001
            failed += 1
    if market_type == "futures":
        try:
            positions = list(await client.fetch_positions() or [])
        except Exception:  # noqa: BLE001
            failed += 1
    return orders, positions, failed
