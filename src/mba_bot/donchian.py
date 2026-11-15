"""donchian_breakout_v1: Donchian channel breakout, PAPER ONLY. Pure functions: no I/O, no clock, no database.

Rules (docs/safety/donchian_breakout_v1.md). Closed candles only; the forming candle is dropped before anything is computed.
* Long: the last closed candle CLOSES above the highest high of the 20 candles before it. Short (Futures only): it closes below their lowest low.
  A wick beyond the channel with a close back inside does not count. Spot is long only.
* Volume: the breakout candle's volume must be above 1.5 x the average volume of those same 20 candles.
* Skip when ATR% is above 3 x its own average over the 20 candles before the breakout candle (a move that is already extreme).
* Stop: the channel midline or 1.5 x ATR from entry, whichever is closer to entry.
* Exit, first event wins: the stop; a 2 x ATR trailing stop (it only ever ratchets towards the trade); or a CLOSE beyond the opposite 10-candle channel
  (below the lowest low of the previous 10 candles for a long, above the highest high for a short). There is no profit target and no time stop.

Nothing here is tuned on data. The numbers are the specification; donchian_backtest.py measures them.
"""
from __future__ import annotations

from dataclasses import dataclass

from .indicators import atr as atr_series

STRATEGY_ID = "donchian_breakout_v1"

EXIT_STOP = "stop_loss"  # the initial protective stop (the cooldown guard counts this reason)
EXIT_TRAIL = "trailing_stop"
EXIT_CHANNEL = "channel_exit"


@dataclass(frozen=True)
class DonchianParams:
    entry_period: int = 20
    exit_period: int = 10
    volume_period: int = 20
    volume_mult: float = 1.5
    atr_period: int = 14
    atr_avg_period: int = 20
    atr_extreme_mult: float = 3.0
    stop_atr: float = 1.5
    trail_atr: float = 2.0


@dataclass(frozen=True)
class DonchianSignal:
    side: str | None  # "long" | "short" | None
    reason: str
    close: float | None = None
    upper: float | None = None
    lower: float | None = None
    mid: float | None = None
    volume: float | None = None
    avg_volume: float | None = None
    atr: float | None = None
    atr_pct: float | None = None
    atr_pct_avg: float | None = None
    stop: float | None = None  # the initial stop for this entry, from initial_stop
    target_atr: float | None = None  # not used: the strategy has no profit target (the fee guard then uses its global proxy)
    gap_atr: float | None = None  # not used by this strategy


def min_candles(p: DonchianParams = DonchianParams()) -> int:
    return max(p.entry_period, p.volume_period, p.atr_period + p.atr_avg_period) + 2


def initial_stop(entry: float, side: str, mid: float, atr: float, p: DonchianParams = DonchianParams()) -> float:
    """The channel midline or stop_atr x ATR from entry, whichever is closer to entry."""
    if side == "long":
        return max(mid, entry - p.stop_atr * atr)
    return min(mid, entry + p.stop_atr * atr)


def channel(candles: list, period: int) -> tuple[float, float]:
    """(highest high, lowest low) of the given candles. The caller passes the `period` candles BEFORE the one being judged."""
    return max(c.high for c in candles), min(c.low for c in candles)


def signal_from_candles(closed: list, p: DonchianParams = DonchianParams(), *, allow_short: bool = False) -> DonchianSignal:
    """The strategy's verdict on the last CLOSED candle. `closed` must already exclude the forming candle (rsi_mr.closed_only does that)."""
    if len(closed) < min_candles(p):
        return DonchianSignal(None, "insufficient candle history")
    atr_values = atr_series([c.high for c in closed], [c.low for c in closed], [c.close for c in closed], p.atr_period)
    return signal_at(closed, atr_values, len(closed) - 1, p, allow_short=allow_short)


def signal_at(closed: list, atr_values: list, i: int, p: DonchianParams = DonchianParams(), *, allow_short: bool = False) -> DonchianSignal:
    """The verdict on candle `i` of `closed`, from an ATR series computed once over the same candles (ATR is causal, so the value at `i` is
    what a prefix would give). The live path and the backtest runner both end up here: there is one rule, not two."""
    if i < min_candles(p) - 1:
        return DonchianSignal(None, "insufficient candle history")
    last = closed[i]
    upper, lower = channel(closed[i - p.entry_period:i], p.entry_period)
    mid = (upper + lower) / 2.0
    atr_now = atr_values[i]
    window = [(a / c.close) for a, c in zip(atr_values[i - p.atr_avg_period:i], closed[i - p.atr_avg_period:i]) if a is not None and c.close]
    if atr_now is None or not last.close or not atr_now > 0 or len(window) < p.atr_avg_period:
        return DonchianSignal(None, "indicator not available")
    atr_pct = atr_now / last.close
    atr_pct_avg = sum(window) / len(window)
    avg_volume = sum(c.volume for c in closed[i - p.volume_period:i]) / p.volume_period
    common = dict(close=last.close, upper=upper, lower=lower, mid=mid, volume=last.volume, avg_volume=avg_volume, atr=atr_now, atr_pct=atr_pct, atr_pct_avg=atr_pct_avg)

    if last.close > upper:
        side = "long"
    elif last.close < lower:
        side = "short"
    else:
        wick = last.high > upper or last.low < lower
        return DonchianSignal(None, "wick beyond the channel, close inside: not a breakout" if wick else "no breakout", **common)
    if side == "short" and not allow_short:
        return DonchianSignal(None, "short breakout on a long-only market", **common)
    if not avg_volume > 0:
        return DonchianSignal(None, "average volume not available", **common)
    if not last.volume > p.volume_mult * avg_volume:
        return DonchianSignal(None, "breakout without volume confirmation", **common)
    if not atr_pct_avg > 0:
        return DonchianSignal(None, "indicator not available", **common)
    if atr_pct > p.atr_extreme_mult * atr_pct_avg:
        return DonchianSignal(None, "ATR% already extreme: skipped", **common)
    return DonchianSignal(side, "close beyond the channel with volume", stop=initial_stop(last.close, side, mid, atr_now, p), **common)


@dataclass(frozen=True)
class Exit:
    reason: str
    price: float
    candle_ts: int


def exit_check(*, side: str, entry: float, stop: float, opened_ms: int, closed: list, tf_ms: int, p: DonchianParams = DonchianParams(),
               atr_values: list | None = None) -> Exit | None:
    """Walk the closed candles that ended after the entry, oldest first, and return the FIRST exit event, or None.

    Per candle: the stop (the initial stop, or the trailing stop once it is tighter) is checked against the candle's low/high FIRST, using the
    level set by the candles before it; then the trailing stop is moved with this candle (extreme since entry -/+ trail_atr x ATR, never loosened);
    then, at the close, the opposite channel. The result depends only on the candles, never on when the cycle happened to run.
    """
    if not closed:
        return None
    if atr_values is None:
        atr_values = atr_series([c.high for c in closed], [c.low for c in closed], [c.close for c in closed], p.atr_period)
    extreme = entry
    trail: float | None = None
    for i, c in enumerate(closed):
        if c.timestamp + tf_ms <= opened_ms:
            continue  # ended before the entry
        if side == "long":
            level = stop if (trail is None or not trail > stop) else trail
            if c.low <= level:
                return Exit(EXIT_STOP if level == stop else EXIT_TRAIL, level, c.timestamp)
            extreme = max(extreme, c.high)
            if atr_values[i] is not None:
                cand = extreme - p.trail_atr * atr_values[i]
                trail = cand if trail is None else max(trail, cand)
            if i >= p.exit_period and c.close < min(x.low for x in closed[i - p.exit_period:i]):
                return Exit(EXIT_CHANNEL, c.close, c.timestamp)
        else:
            level = stop if (trail is None or not trail < stop) else trail
            if c.high >= level:
                return Exit(EXIT_STOP if level == stop else EXIT_TRAIL, level, c.timestamp)
            extreme = min(extreme, c.low)
            if atr_values[i] is not None:
                cand = extreme + p.trail_atr * atr_values[i]
                trail = cand if trail is None else min(trail, cand)
            if i >= p.exit_period and c.close > max(x.high for x in closed[i - p.exit_period:i]):
                return Exit(EXIT_CHANNEL, c.close, c.timestamp)
    return None
