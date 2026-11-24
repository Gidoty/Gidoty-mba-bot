"""The account's real taker fee, when it can be read (docs/safety/net_pnl.md). Used by the staged profit lock, the time stop and the take profit for the cost buffer.

Read-only, cached in memory per account for 24 hours (a failed read is cached for 1 hour so a broken endpoint is not hit every round), and never raises: when the real rate is not known the caller uses the assumed
rate FEE_GUARD_FEE_BPS (5 bps, the standard USD-M taker fee). Nothing is stored in the database."""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

TTL_OK_SECONDS = 24 * 3600
TTL_FAIL_SECONDS = 3600
_cache: dict[str, tuple[float, float | None]] = {}


def reset_cache() -> None:
    _cache.clear()


async def taker_bps(venue, account_id: str, *, assumed_bps: float, clock=time.monotonic) -> tuple[float, str]:
    """(taker fee in basis points per side, source) where source is "account" (read from the exchange) or "assumed". Never raises."""
    now = clock()
    hit = _cache.get(account_id)
    if hit is not None and now - hit[0] < (TTL_OK_SECONDS if hit[1] is not None else TTL_FAIL_SECONDS):
        return (hit[1], "account") if hit[1] is not None else (assumed_bps, "assumed")
    bps: float | None = None
    try:
        rate = await venue.taker_fee_rate()
        bps = rate * 10_000.0 if rate is not None else None
    except Exception as exc:  # noqa: BLE001 - a fee read must never affect trading
        logger.info("account %s: could not read the taker fee (%s); the assumed %.1f bps is used", account_id, type(exc).__name__, assumed_bps)
    _cache[account_id] = (now, bps)
    return (bps, "account") if bps is not None else (assumed_bps, "assumed")
