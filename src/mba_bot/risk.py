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


# Binance USDⓈ-M leverage is an integer from 1 to 125. Anything outside is a misread, not a setting.
LEVERAGE_MIN = 1.0
LEVERAGE_MAX = 125.0

# Reason codes (also the decision-log reason codes).
OK = "ok"
STOP_UNVIABLE = "stop_beyond_safe_liquidation_distance"
INPUTS_INVALID = "protection_check_inputs_invalid"
# The bracket table could not be used. Three different causes, three codes (before, all three were one code, TIERS_UNAVAILABLE, which stays defined because historical decision-log rows and
# older queries carry it; nothing writes it any more):
TIERS_UNAVAILABLE = "maintenance_tiers_unavailable"  # legacy, never produced by assess_stop_viability any more
TIERS_READ_FAILED = "maintenance_tiers_read_failed"  # the brackets could not be read from Binance (call failed, empty, key mismatch, a field missing)
TIERS_INVALID = "maintenance_tiers_invalid"  # they were read but the table is not plausible (gap, overlap, falling rate, impossible number)
TIERS_NO_MATCH = "maintenance_tiers_no_match"  # a plausible table, but no bracket contains this entry's position value
TIERS_REASONS = (TIERS_UNAVAILABLE, TIERS_READ_FAILED, TIERS_INVALID, TIERS_NO_MATCH)
MARGIN_MODE_UNKNOWN = "margin_mode_unknown"
CROSS_UNVERIFIABLE = "cross_margin_unverifiable"


def leverage_is_sane(value, max_leverage: float = LEVERAGE_MAX) -> bool:
    """A real number from LEVERAGE_MIN to `max_leverage`. Booleans, strings and non-finite values are not leverage."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    v = float(value)
    return math.isfinite(v) and LEVERAGE_MIN <= v <= max_leverage


@dataclass(frozen=True)
class MaintenanceTier:
    """One Binance leverage bracket: the notional range [floor, cap], its maintenance-margin rate and the
    maintenance amount (`cum`). Maintenance margin at notional N is  N * rate - cum."""

    tier: int | None
    floor: float
    cap: float
    rate: float
    cum: float


MAX_TIER_RATE = 0.5  # a maintenance-margin rate is a fraction of position value; Binance's top brackets reach 0.5 (UNVERIFIED from memory), nothing above is possible


def tiers_problem(tiers) -> str | None:
    """None when the tiers form a plausible, contiguous bracket table, else a short plain description of the FIRST check that failed (for the journal; it never carries exchange text)."""
    if not tiers:
        return "empty table"
    try:
        if not all(isinstance(t, MaintenanceTier) for t in tiers):
            return "an entry is not a bracket"
        out = sorted(tiers, key=lambda t: t.floor)
        for i, t in enumerate(out, 1):
            vals = (t.floor, t.cap, t.rate, t.cum)
            if any(isinstance(v, bool) or not math.isfinite(float(v)) for v in vals):
                return f"bracket {i}: a value is not a finite number"
            if t.floor < 0:
                return f"bracket {i}: floor {t.floor:g} is below 0"
            if t.cap <= t.floor:
                return f"bracket {i}: cap {t.cap:g} is not above floor {t.floor:g}"
            if not (0 <= t.rate <= MAX_TIER_RATE):
                return f"bracket {i}: rate {t.rate:g} is outside 0 to {MAX_TIER_RATE:g}"
            if t.cum < 0:
                return f"bracket {i}: maintenance amount {t.cum:g} is below 0"
        if out[0].floor != 0:
            return f"first bracket starts at {out[0].floor:g}, not 0"
        for i, (a, b) in enumerate(zip(out, out[1:]), 1):
            if abs(b.floor - a.cap) > 1e-6 * max(1.0, a.cap):  # a gap or an overlap
                return f"brackets {i} and {i + 1}: cap {a.cap:g} and next floor {b.floor:g} do not meet"
            if b.rate < a.rate:  # rates only rise with size
                return f"brackets {i} and {i + 1}: rate falls from {a.rate:g} to {b.rate:g}"
        return None
    except (TypeError, ValueError, AttributeError):
        return "a value could not be read as a number"


def validate_tiers(tiers) -> list[MaintenanceTier] | None:
    """The tiers sorted by floor if they form a plausible, contiguous bracket table, else None (fail closed). `tiers_problem` says which check failed."""
    if tiers_problem(tiers) is not None:
        return None
    return sorted(tiers, key=lambda t: t.floor)


def no_match_note(table: list[MaintenanceTier], notional: float) -> str:
    """For the journal: the position value and the brackets that did not contain it (floors and caps, at most 8)."""
    shown = ", ".join(f"[{t.floor:g}-{t.cap:g}]" for t in table[:8])
    more = f" (+{len(table) - 8} more)" if len(table) > 8 else ""
    return f"position value {notional:g} is in none of the brackets {shown}{more}"


@dataclass(frozen=True)
class StopViability:
    viable: bool
    reason: str
    # All distances are FRACTIONS of the entry price (0.02 = 2%), the same unit as trade_telemetry.stop_distance_pct.
    stop_distance_frac: float | None = None
    liquidation_distance_frac: float | None = None
    max_allowed_stop_frac: float | None = None
    margin_mode: str | None = None
    tier: int | None = None
    tier_rate: float | None = None
    tier_cum: float | None = None
    notional: float | None = None
    effective_maint_rate: float | None = None
    margin_basis: str | None = None  # "isolated" | "cross"
    note: str | None = None  # for a refusal about the bracket table: the first failing check, or why no bracket matched (numbers only, no exchange text)
    tiers_source: str | None = None  # "fresh" (read from Binance now) or "cache" (last known good, within the maximum age); set by the caller that read the brackets
    tiers_age_seconds: float | None = None  # how old the cached table is, when tiers_source is "cache"

    def detail(self) -> dict:
        """Small, non-sensitive numbers for the decision log (fractions, not percent)."""
        keys = (
            "stop_distance_frac", "liquidation_distance_frac", "max_allowed_stop_frac", "margin_mode", "tier",
            "tier_rate", "tier_cum", "notional", "effective_maint_rate", "margin_basis", "note", "tiers_source", "tiers_age_seconds",
        )
        return {k: getattr(self, k) for k in keys}


def assess_stop_viability(
    *,
    side: str,
    entry_price: float,
    stop_price: float,
    quantity: float,
    leverage: float,
    effective_capital: float,
    margin_mode: str | None,
    tiers,
    other_open_positions: int | None,
    min_maint_rate: float,
    safety_fraction: float,
    cross_collateral_factor: float,
    max_leverage: float = LEVERAGE_MAX,
) -> StopViability:
    """Futures, before an entry order. Can a protective stop sit safely inside the liquidation price?

    Single position, one-way mode, USDT-margined. With N = quantity * entry, the matched Binance bracket
    (rate, cum) and the effective maintenance rate  t = max(rate - cum / N, min_maint_rate):

      margin ratio m:  isolated  1 / leverage
                       cross     max(1 / leverage, cross_collateral_factor * effective_capital / N)
      distance to liquidation:  long  (m - t);   short  (m - t) / (1 + rate)
      enter only if  distance > 0  and  |entry - stop| / entry  <=  safety_fraction * distance   (equality passes)

    This is the standard isolated-margin liquidation formula for one position, derived from the maintenance-margin
    model (MM = N * rate - cum); it has NOT been checked against an official Binance page by this code's authors.
    For a long the exact denominator would be (1 - rate); it is dropped, which makes the distance smaller (safer).
    Fees, funding and the liquidation clearance fee are ignored: `safety_fraction` and `min_maint_rate` are the margin.

    Fails closed (viable=False, with a specific reason) when anything needed is missing or implausible: prices,
    quantity, leverage outside 1..max_leverage (default 125), the margin mode (unknown is NOT assumed), the bracket table or a bracket for
    N, and, in cross margin, any other open position on the account or an unknown position count.
    """
    def fail(reason: str, **kw) -> StopViability:
        return StopViability(False, reason, margin_mode=margin_mode if margin_mode in ("isolated", "cross") else None, **kw)

    try:
        nums = (entry_price, stop_price, quantity, effective_capital, min_maint_rate, safety_fraction, cross_collateral_factor)
        if any(isinstance(v, bool) or v is None or not math.isfinite(float(v)) for v in nums):
            return fail(INPUTS_INVALID)
    except (TypeError, ValueError):
        return fail(INPUTS_INVALID)
    if entry_price <= 0 or stop_price <= 0 or quantity <= 0 or effective_capital <= 0:
        return fail(INPUTS_INVALID)
    if not leverage_is_sane(leverage, max_leverage):
        return fail(INPUTS_INVALID)
    if not (0 <= min_maint_rate < 0.5) or not (0 < safety_fraction <= 1) or not (0 < cross_collateral_factor <= 1):
        return fail(INPUTS_INVALID)
    if side not in ("long", "short"):
        return fail(INPUTS_INVALID)
    if side == "long" and stop_price >= entry_price:
        return fail(INPUTS_INVALID)
    if side == "short" and stop_price <= entry_price:
        return fail(INPUTS_INVALID)
    if margin_mode not in ("isolated", "cross"):
        return fail(MARGIN_MODE_UNKNOWN)

    notional = quantity * entry_price
    if tiers is None:
        return fail(TIERS_READ_FAILED, notional=notional)  # the caller could not read the brackets (why is in the journal)
    problem = tiers_problem(tiers)
    if problem is not None:
        return fail(TIERS_INVALID, notional=notional, note=problem)
    table = validate_tiers(tiers)
    matches = [t for t in table if t.floor <= notional <= t.cap]  # at a boundary both brackets match: take the worse
    if not matches:
        return fail(TIERS_NO_MATCH, notional=notional, note=no_match_note(table, notional))
    tier = max(matches, key=lambda t: t.rate - t.cum / notional)
    t_eff = max(tier.rate - tier.cum / notional, min_maint_rate)

    m = 1.0 / leverage
    basis = "isolated"
    if margin_mode == "cross":
        if other_open_positions is None or isinstance(other_open_positions, bool) or other_open_positions != 0:
            return fail(CROSS_UNVERIFIABLE, notional=notional, tier=tier.tier, tier_rate=tier.rate, tier_cum=tier.cum)
        m = max(m, cross_collateral_factor * effective_capital / notional)
        basis = "cross"

    dist = m - t_eff
    if side == "short":
        dist /= 1.0 + tier.rate
    stop_dist = abs(entry_price - stop_price) / entry_price
    allowed = max(0.0, safety_fraction * dist)
    ok = dist > 0 and stop_dist <= allowed
    return StopViability(
        ok, OK if ok else STOP_UNVIABLE,
        stop_distance_frac=round(stop_dist, 6), liquidation_distance_frac=round(dist, 6),
        max_allowed_stop_frac=round(allowed, 6), margin_mode=margin_mode, tier=tier.tier, tier_rate=tier.rate,
        tier_cum=tier.cum, notional=round(notional, 4), effective_maint_rate=round(t_eff, 6), margin_basis=basis,
    )
