"""Position sizing and stop-loss placement.

Every trade risks a fixed percentage of account balance, sized off the
distance to an ATR-based stop - never a fixed coin quantity. This is what
keeps a string of losses from compounding into an outsized drawdown.
"""
from __future__ import annotations

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
    `risk_pct` of `account_balance` (before fees/slippage)."""
    if account_balance <= 0:
        raise InvalidRiskParameters(f"account_balance must be positive, got {account_balance}")

    effective_risk_pct = clamp_risk_pct(risk_pct, max_risk_pct=max_risk_pct)
    stop_distance = abs(entry_price - stop_price)
    if stop_distance <= 0:
        raise InvalidRiskParameters("stop_price must differ from entry_price")

    risk_amount = account_balance * (effective_risk_pct / 100)
    quantity = risk_amount / stop_distance
    return PositionSize(quantity=quantity, risk_amount=risk_amount, stop_distance=stop_distance)
