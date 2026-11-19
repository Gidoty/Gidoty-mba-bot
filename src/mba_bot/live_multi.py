"""Live multi-position (docs/safety/live_multi_position.md). Ships behind LIVE_MULTI_POSITION (default off). Pure functions: no I/O, no clock, no database.

With the switch off a LIVE account holds one trade at a time, exactly as before. With it on, an account may hold up to its own `live_slots` (1 to HARD_MAX) trades at once, one per pair, taken in the
priority Donchian, then EMA, then RSI, at most one new entry per cycle. The account-wide limits (daily dollar limit, 50% total stop, kill switch) already measure the whole account and apply to every entry.
The extra safety rules here cap the OPEN RISK across the trades and share the trading capital between them."""
from __future__ import annotations

import math

HARD_MAX = 3
R_TOTAL_RISK = "live_total_open_risk_cap"
R_DAILY_LIMIT = "live_open_risk_above_daily_limit"
R_CAPITAL_SHORT = "live_free_capital_short"


def _ok(x) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def slot_cap(configured, *, switch_on: bool) -> int:
    """How many live trades an account may hold: 1 unless the switch is on; then its own setting clamped to 1..HARD_MAX (empty, bad or non-integer data is 1)."""
    if not switch_on:
        return 1
    if isinstance(configured, bool) or not isinstance(configured, int):
        return 1
    return max(1, min(HARD_MAX, configured))


def trade_risk(trade) -> float:
    """The most an open trade can lose at its stop as it rests NOW: quantity x distance from entry to the current protective stop (a trailed stop lowers it; a stop beyond break-even counts 0)."""
    qty, entry, stop = getattr(trade, "quantity", None), getattr(trade, "entry_price", None), getattr(trade, "stop_loss_price", None)
    if not (_ok(qty) and _ok(entry) and _ok(stop)):
        return float("inf")  # unknown risk is treated as unbounded: an unreadable trade blocks additional entries (fail closed)
    side = getattr(trade, "side", "long")
    distance = (entry - stop) if side == "long" else (stop - entry)
    return max(0.0, float(qty) * float(distance))


def total_open_risk(trades) -> float:
    return sum(trade_risk(t) for t in trades)


def committed_capital(trades, leverage_cap: float) -> float:
    """Trading capital the open trades already use: their position value divided by the leverage cap on Futures (1 on Spot)."""
    cap = float(leverage_cap) if _ok(leverage_cap) and leverage_cap >= 1 else 1.0
    total = 0.0
    for t in trades:
        qty, entry = getattr(t, "quantity", None), getattr(t, "entry_price", None)
        if not (_ok(qty) and _ok(entry)):
            return float("inf")
        total += float(qty) * float(entry) / cap
    return total


def check_additional_entry(open_trades, *, new_risk: float, capital, cap_pct, daily_limit_usd) -> str | None:
    """Only for a SECOND or THIRD trade (the first is judged by the existing rules): a reason code if the entry must be refused, else None.
      * total open risk (every open trade at its current stop plus this one) above `cap_pct` percent of the trading capital;
      * total open risk above the customer's daily dollar limit, so all stops hit together cannot exceed what the daily limit allows.
    Any input that is not a usable number refuses (fail closed)."""
    if not open_trades:
        return None
    if not (_ok(new_risk) and new_risk >= 0 and _ok(capital) and capital > 0):
        return R_TOTAL_RISK
    total = total_open_risk(open_trades) + float(new_risk)
    if not math.isfinite(total):
        return R_TOTAL_RISK
    if _ok(cap_pct) and cap_pct > 0 and total > float(capital) * float(cap_pct) / 100.0 + 1e-9:
        return R_TOTAL_RISK
    if _ok(daily_limit_usd) and daily_limit_usd > 0 and total > float(daily_limit_usd) + 1e-9:
        return R_DAILY_LIMIT
    return None


def free_capital(allocated, open_trades, leverage_cap: float) -> float:
    """The trading capital left for a new trade: the allocation minus what the open trades use, never below 0."""
    if not _ok(allocated):
        return 0.0
    used = committed_capital(open_trades, leverage_cap)
    return max(0.0, float(allocated) - used) if math.isfinite(used) else 0.0


def live_pairs(account) -> list[str]:
    """The pairs a live multi-position account may trade: its own pair first, then the owner-set pair list, at most HARD_MAX, no duplicates (the same list paper multi-trade uses)."""
    from .multi_trade import trading_pairs

    return trading_pairs(account)


MSG_SLOTS_OFF = "holding more than one live trade at a time is not available on this service"
MSG_SLOTS_AUTO = "Auto Select trades one pair at a time. Switch pair selection to Manual to hold more than one trade at once."
MSG_AUTO_SLOTS = "Auto Select trades one pair at a time. Set the number of open trades to 1 before switching pair selection to Auto Select."


def slots_refusal(*, new_slots, new_selection_mode, current_slots, current_selection_mode, switch_on: bool):
    """The ONE rule for the slot setting, used by the account API (which refuses with it) and by the setup advisor (which reports it): (http status, plain text) or None.
    `new_slots` and `new_selection_mode` are what this request sets (None = not in the request); the current values are what the account has now."""
    if new_slots is not None:
        if not switch_on:
            return 409, MSG_SLOTS_OFF
        if new_slots > 1 and (new_selection_mode or current_selection_mode) == "auto":
            return 422, MSG_SLOTS_AUTO
    elif switch_on and slot_cap(current_slots, switch_on=True) > 1 and new_selection_mode == "auto":
        return 422, MSG_AUTO_SLOTS
    return None
