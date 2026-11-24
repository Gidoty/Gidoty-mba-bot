"""The trailing-stop level for a LIVE donchian_breakout_v1 trade (docs/safety/live_donchian.md). Pure: no I/O, no clock, no database.

Kept out of donchian.py on purpose: that file is pinned by the strategy's pre-registered backtest plan (its SHA-256 is part of the plan hash), so changing it would void the registration. This module
only reads the strategy's own parameters and repeats the same ratchet `donchian.exit_check` walks."""
from __future__ import annotations

from .donchian import DonchianParams
from .indicators import atr as atr_series


def trailing_level(*, side: str, entry: float, opened_ms: int, closed: list, p: DonchianParams = DonchianParams(), atr_values: list | None = None) -> float | None:
    """The trailing-stop level the closed candles after the entry have earned so far: for a long, the highest high since entry minus trail_atr x ATR, ratcheted (never lowered); for a short the mirror
    image. None until a candle with a usable ATR has closed after the entry. It is the same ratchet `donchian.exit_check` walks, so a live stop moved to this level is the level the paper rules would trade.
    The caller moves a protective stop to it only if it is better than the stop already resting."""
    if not closed:
        return None
    if atr_values is None:
        atr_values = atr_series([c.high for c in closed], [c.low for c in closed], [c.close for c in closed], p.atr_period)
    extreme = entry
    trail: float | None = None
    for i, c in enumerate(closed):
        if c.timestamp < opened_ms:
            continue
        if side == "long":
            extreme = max(extreme, c.high)
            if atr_values[i] is not None:
                cand = extreme - p.trail_atr * atr_values[i]
                trail = cand if trail is None else max(trail, cand)
        else:
            extreme = min(extreme, c.low)
            if atr_values[i] is not None:
                cand = extreme + p.trail_atr * atr_values[i]
                trail = cand if trail is None else min(trail, cand)
    return trail


def latest_atr(closed: list, p: DonchianParams = DonchianParams()) -> float | None:
    """The ATR of the latest closed candle, or None when there is not enough history."""
    if not closed:
        return None
    vals = atr_series([c.high for c in closed], [c.low for c in closed], [c.close for c in closed], p.atr_period)
    return vals[-1]


def running_level(*, side: str, peak: float | None, atr: float | None, p: DonchianParams = DonchianParams()) -> float | None:
    """The trailing level worked out from the RUNNING best price (every round, not only at a candle close): the best price since entry minus trail_atr x ATR for a long, plus for a short. None without both.
    The caller never loosens a stop, so a level below the stop resting is simply not used."""
    if peak is None or atr is None or atr <= 0:
        return None
    return peak - p.trail_atr * atr if side == "long" else peak + p.trail_atr * atr
