"""Position sizing and stop-loss placement.

Every trade risks a fixed percentage of account balance, sized off the
distance to an ATR-based stop - never a fixed coin quantity. This is what
keeps a string of losses from compounding into an outsized drawdown.
"""
from __future__ import annotations

import math
from dataclasses import dataclass


class InvalidRiskParameters(ValueError):
    pass


def clamp_risk_pct(risk_pct: float, *, max_risk_pct: float) -> float:
    """Never let a stored/customer-set risk % exceed the hard cap, even if
    bad data somehow made it into the database."""
    if risk_pct <= 0:
        raise InvalidRiskParameters(f"risk_pct must be positive, got {risk_pct}")
    return min(risk_pct, max_risk_pct)


def compute_stop_loss(*, entry_price: float, atr: float, side: str, atr_multiplier: float) -> float:
    """`side` is 'long' or 'short'. Stop sits `atr_multiplier * ATR` away
    from entry, on the losing side of the trade."""
    if entry_price <= 0:
        raise InvalidRiskParameters(f"entry_price must be positive, got {entry_price}")
    if atr <= 0:
        raise InvalidRiskParameters(f"atr must be positive, got {atr}")
    if side not in ("long", "short"):
        raise InvalidRiskParameters(f"side must be 'long' or 'short', got {side!r}")

    offset = atr * atr_multiplier
    if side == "long":
        stop = entry_price - offset
    else:
        stop = entry_price + offset

    if stop <= 0:
        raise InvalidRiskParameters(
            f"computed stop price {stop} is non-positive (entry={entry_price}, atr={atr}, multiplier={atr_multiplier})"
        )
    return stop


@dataclass(frozen=True)
class PositionSize:
    quantity: float
    risk_amount: float
    stop_distance: float


def compute_position_size(
    *, account_balance: float, risk_pct: float, entry_price: float, stop_price: float, max_risk_pct: float
) -> PositionSize:
    """Quantity such that if the stop is hit, the loss equals exactly
    `risk_pct` of `account_balance` (before fees/slippage). `account_balance`
    is the caller's *effective capital* - for a live account that's
    `min(allocated_capital, live Binance balance)`, never the raw
    exchange balance alone (see trader.py's _maybe_enter)."""
    if account_balance <= 0:
        raise InvalidRiskParameters(f"account_balance must be positive, got {account_balance}")

    effective_risk_pct = clamp_risk_pct(risk_pct, max_risk_pct=max_risk_pct)
    stop_distance = abs(entry_price - stop_price)
    if stop_distance <= 0:
        raise InvalidRiskParameters("stop_price must differ from entry_price")

    risk_amount = account_balance * (effective_risk_pct / 100)
    quantity = risk_amount / stop_distance
    return PositionSize(quantity=quantity, risk_amount=risk_amount, stop_distance=stop_distance)


def _resized(size: PositionSize, *, quantity: float) -> PositionSize:
    """Shrinks a PositionSize to a smaller quantity, recomputing
    risk_amount so it always reflects the capital actually at risk -
    never the larger, pre-ceiling figure the raw risk-% formula implied."""
    return PositionSize(quantity=quantity, risk_amount=quantity * size.stop_distance, stop_distance=size.stop_distance)


def apply_exposure_ceiling(
    size: PositionSize, *, entry_price: float, effective_capital: float, max_notional_multiplier: float
) -> PositionSize:
    """Caps notional exposure (quantity * entry_price) at
    `effective_capital * max_notional_multiplier`, trimming quantity
    down - never up - if the risk-% formula above would otherwise
    produce a larger position than this conservative ceiling allows.

    This exists because risk-based sizing alone has no opinion on
    notional: a tighter stop (smaller ATR) produces a *larger* quantity
    for the same dollar risk, and nothing in compute_position_size caps
    how large that gets. A gap past the stop (slippage, a fast market)
    turns the loss from "risk_amount" into "quantity * price move",
    which is unbounded unless something caps quantity directly.

    `max_notional_multiplier=1.0` is the natural ceiling for Spot (you
    can never spend more than you have - no leverage exists there in
    this system), so this function is a no-op in practice for Spot
    whenever the operator leaves it at 1.0. For Futures, where leverage
    could otherwise let a much larger notional fit within the same
    margin, this is the actual binding safety limit - deliberately a
    fixed, conservative, operator-configured ceiling (`Settings.
    max_notional_multiplier`), independent of whatever leverage is
    actually configured on the exchange account for that symbol. See
    `apply_margin_ceiling` for the separate, complementary check that
    *does* use the account's real leverage, read-only."""
    if max_notional_multiplier <= 0:
        raise InvalidRiskParameters(f"max_notional_multiplier must be positive, got {max_notional_multiplier}")
    max_notional = effective_capital * max_notional_multiplier
    notional = size.quantity * entry_price
    if notional <= max_notional:
        return size
    return _resized(size, quantity=max_notional / entry_price)


def apply_margin_ceiling(size: PositionSize, *, entry_price: float, effective_capital: float, leverage: float) -> PositionSize:
    """Futures only. Caps quantity so the margin required to open the
    position (`notional / leverage`) never exceeds `effective_capital` -
    the capital the customer allocated to the bot, not their whole
    Binance account. `leverage` must be the account's ACTUAL, currently
    configured leverage for this symbol, read directly from Binance
    (see exchange.fetch_symbol_leverage - read-only; this module never
    sets leverage, and never assumes a value it hasn't verified)."""
    if leverage <= 0:
        raise InvalidRiskParameters(f"leverage must be positive, got {leverage}")
    notional = size.quantity * entry_price
    required_margin = notional / leverage
    if required_margin <= effective_capital:
        return size
    capped_notional = effective_capital * leverage
    return _resized(size, quantity=capped_notional / entry_price)


@dataclass(frozen=True)
class StopViability:
    viable: bool
    reason: str  # "ok" | "stop_beyond_safe_liquidation_distance" | "protection_check_inputs_invalid"
    stop_distance_pct: float | None = None
    liquidation_distance_pct: float | None = None
    max_allowed_stop_pct: float | None = None
    margin_mode_assumed: str | None = None

    def detail(self) -> dict:
        """Small, non-sensitive numbers for the decision log."""
        return {
            "stop_distance_pct": self.stop_distance_pct,
            "est_liquidation_distance_pct": self.liquidation_distance_pct,
            "max_allowed_stop_pct": self.max_allowed_stop_pct,
            "margin_mode_assumed": self.margin_mode_assumed,
        }


def assess_stop_viability(
    *,
    side: str,
    entry_price: float,
    stop_price: float,
    quantity: float,
    leverage: float,
    effective_capital: float,
    margin_mode: str | None,
    maint_margin_rate: float,
    safety_fraction: float,
) -> StopViability:
    """Futures, before an entry order. Can a protective stop sit safely inside the liquidation price?

    Estimated distance from entry to liquidation, as a fraction of entry (long and short are symmetric):

      isolated (also used when the margin mode is unknown):  1/leverage - maintenance_margin_rate
      cross:   max(isolated, effective_capital / notional - maintenance_margin_rate)

    The entry is allowed only if  |entry - stop| / entry  <=  safety_fraction * distance.  Equality passes.
    The estimate ignores fees, funding and the tiered maintenance rate at large notionals, which is why the
    assumed rate defaults above Binance's lowest tier and only `safety_fraction` of the distance may be used.
    Any input that is missing, non-finite, non-positive, or on the wrong side of entry fails closed.
    """
    def invalid() -> StopViability:
        return StopViability(False, "protection_check_inputs_invalid", margin_mode_assumed=margin_mode)

    nums = (entry_price, stop_price, quantity, leverage, effective_capital, maint_margin_rate, safety_fraction)
    try:
        if any(v is None or not math.isfinite(float(v)) for v in nums):
            return invalid()
    except (TypeError, ValueError):
        return invalid()
    if entry_price <= 0 or stop_price <= 0 or quantity <= 0 or leverage <= 0 or effective_capital <= 0:
        return invalid()
    if not (0 <= maint_margin_rate < 0.5) or not (0 < safety_fraction <= 1):
        return invalid()
    if side == "long" and stop_price >= entry_price:
        return invalid()
    if side == "short" and stop_price <= entry_price:
        return invalid()
    if side not in ("long", "short"):
        return invalid()

    mode = margin_mode if margin_mode in ("isolated", "cross") else "isolated"
    stop_dist = abs(entry_price - stop_price) / entry_price
    liq_dist = 1.0 / leverage - maint_margin_rate
    if mode == "cross":
        liq_dist = max(liq_dist, effective_capital / (quantity * entry_price) - maint_margin_rate)
    allowed = max(0.0, safety_fraction * liq_dist)
    ok = liq_dist > 0 and stop_dist <= allowed
    return StopViability(
        ok,
        "ok" if ok else "stop_beyond_safe_liquidation_distance",
        stop_distance_pct=round(stop_dist * 100, 4),
        liquidation_distance_pct=round(liq_dist * 100, 4),
        max_allowed_stop_pct=round(allowed * 100, 4),
        margin_mode_assumed=mode,
    )
