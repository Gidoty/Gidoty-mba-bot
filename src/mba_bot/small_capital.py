"""Small-capital pairs (docs/safety/small_capital_pairs.md). Ships behind SMALL_CAPITAL_PAIRS (default off).

When an account's trading capital and leverage cap cannot meet a pair's minimum order size, the bot must not skip the pair silently: it records a skip reason per pair and day, the customer
sees a plain message with the minimum capital needed at their leverage cap, and Auto Select scans only pairs that are viable for that account.

The minimum order is read ONLY from the exchange filters the bot already loads (MIN_NOTIONAL / NOTIONAL and the quantity step or minimum quantity); nothing is assumed from memory. The check is a
NECESSARY condition (capital x leverage cap against the smallest order the exchange accepts): it never marks a pair unviable that the entry path could trade, and the entry path keeps its own exact checks.

Pure functions first, then the small amount of database work (one upserted row per account, pair, reason and UTC day)."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import PairSkip, TradingAccount

R_BELOW_MIN_CAPITAL = "pair_below_minimum_capital"
R_BELOW_FILTERS = "pair_below_exchange_filters"
REASONS = (R_BELOW_MIN_CAPITAL, R_BELOW_FILTERS)


def _finite(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


@dataclass(frozen=True)
class Viability:
    viable: bool | None  # None = the exchange minimum could not be read (the entry path still checks in full)
    min_order_value: float | None  # the smallest order, in quote currency, the exchange accepts for this pair
    min_capital: float | None  # trading capital needed to reach it at the leverage cap
    leverage_cap: float
    capital: float | None


def leverage_cap_for(market_type: str, customer_cap) -> float:
    """The multiple of capital a position may be worth: the customer's leverage cap on Futures, 1 on Spot."""
    return float(customer_cap) if market_type == "futures" and _finite(customer_cap) and customer_cap >= 1 else 1.0


def min_order_value(rules, price) -> float | None:
    """The smallest order value the exchange accepts: the larger of the minimum notional and the smallest quantity (market minimum, at least one step) at `price`. A part that cannot be
    computed (no minimum notional given, or no usable price for the quantity part) is left out; if neither can be computed the answer is None."""
    parts = []
    if rules is not None and _finite(getattr(rules, "min_notional", None)) and rules.min_notional > 0:
        parts.append(float(rules.min_notional))
    if rules is not None and _finite(price) and price > 0:
        qty = max(float(getattr(rules, "market_min_qty", 0) or 0), float(getattr(rules, "step", 0) or 0))
        if qty > 0:
            parts.append(qty * float(price))
    return max(parts) if parts else None


def assess(rules, price, *, capital, leverage_cap: float) -> Viability:
    floor = min_order_value(rules, price)
    cap = float(leverage_cap) if _finite(leverage_cap) and leverage_cap >= 1 else 1.0
    if floor is None or not _finite(capital):
        return Viability(None, floor, None if floor is None else floor / cap, cap, float(capital) if _finite(capital) else None)
    return Viability(capital * cap >= floor - 1e-9, floor, floor / cap, cap, float(capital))


def message(pair: str, v: Viability) -> str:
    """The plain customer text. Numbers only, from the exchange filters and the customer's own settings."""
    need = f"{math.ceil(v.min_capital * 100) / 100:.2f}" if _finite(v.min_capital) else "more"
    cap = f"{v.leverage_cap:g}x"
    have = f" Your trading capital is {v.capital:.2f} USDT." if _finite(v.capital) else ""
    return (f"{pair} is too small for your trading capital. Its smallest order needs about {need} USDT of trading capital at your {cap} leverage limit.{have} "
            "Choose another pair or raise your trading capital.")


# ---------------------------------------------------------------------------------------------------- database
def record_skip(session: Session, account: TradingAccount, pair: str, reason: str, v: Viability, *, now: datetime | None = None) -> PairSkip:
    """One row per account, pair, reason and UTC day; later skips the same day count up and refresh the numbers. Never raises into the caller's decision (the caller wraps it)."""
    now = now or datetime.now(timezone.utc)
    day = now.astimezone(timezone.utc).date().isoformat()
    row = session.execute(select(PairSkip).where(PairSkip.account_id == account.id, PairSkip.pair == pair, PairSkip.reason == reason, PairSkip.day_key == day)).scalar_one_or_none()
    if row is None:
        row = PairSkip(account_id=account.id, pair=pair, reason=reason, day_key=day, count=0, first_seen=now)
        session.add(row)
    row.count = (row.count or 0) + 1
    row.last_seen = now
    row.capital = v.capital if _finite(v.capital) else None
    row.leverage_cap = v.leverage_cap
    row.min_capital = v.min_capital if _finite(v.min_capital) else None
    session.flush()
    return row


def latest_for_pair(session: Session, account_id: str, pair: str, *, now: datetime | None = None, hours: float = 24.0) -> PairSkip | None:
    """The newest skip of the account's own pair in the last `hours` (for the dashboard and Bot Settings message)."""
    from datetime import timedelta

    now = now or datetime.now(timezone.utc)
    return session.execute(
        select(PairSkip).where(PairSkip.account_id == account_id, PairSkip.pair == pair, PairSkip.last_seen >= now - timedelta(hours=hours)).order_by(PairSkip.last_seen.desc()).limit(1)
    ).scalar_one_or_none()


def row_message(row: PairSkip) -> str:
    return message(row.pair, Viability(False, None, row.min_capital, row.leverage_cap or 1.0, row.capital))
