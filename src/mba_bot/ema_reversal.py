"""Reversal exits for EMA trades (docs/safety/ema_reversal_exits.md). Pure: no I/O, no clock, no database. Both rules are OFF by default and are judged on CLOSED candles only.

(1) Fast-EMA cross: after the trade has been in profit (its best price at least `min_profit_r` x R beyond the entry, R = the distance from the entry to the initial stop), a candle that CLOSES back across the fast EMA
    (below it for a long, above it for a short) ends the trade.
(2) Gap shrink: the fast/slow gap in the trade's favour (fast minus slow for a long, slow minus fast for a short), measured on every closed candle since the entry; once its peak is positive, a candle whose gap has
    fallen to (1 - gap_fraction) of that peak or less ends the trade.
The exit itself is the caller's: lifecycle.execute_exit for a live trade, a close at the price for a paper trade."""
from __future__ import annotations

from dataclasses import dataclass

from .indicators import ema as ema_series

REASON_FAST = "reversal_fast_ema"
REASON_GAP = "reversal_gap"


@dataclass(frozen=True)
class ReversalRules:
    fast_cross: bool = False
    gap_fraction: float = 0.0
    min_profit_r: float = 0.5

    @property
    def any_on(self) -> bool:
        return self.fast_cross or self.gap_fraction > 0


def rules_from_settings(settings) -> ReversalRules:
    return ReversalRules(fast_cross=settings.ema_reversal_fast_cross, gap_fraction=settings.ema_reversal_gap_fraction, min_profit_r=settings.ema_reversal_min_profit_r)


def fast_cross_hit(side: str, *, close: float, fast: float | None, peak: float | None, entry: float, initial_stop: float, min_profit_r: float) -> bool:
    """True when the trade has been in profit (best price at least min_profit_r x R beyond the entry) and the close is back across the fast EMA (strictly below it for a long, above it for a short)."""
    r = abs(entry - initial_stop)
    if fast is None or peak is None or not r > 0:
        return False
    gain = (peak - entry) if side == "long" else (entry - peak)
    if gain < min_profit_r * r:
        return False
    return close < fast if side == "long" else close > fast


def gap_shrink_hit(side: str, *, gap_now: float | None, gap_peak: float | None, fraction: float) -> bool:
    """True when the peak gap since entry was positive and the gap now is at or below (1 - fraction) of it. Off at fraction 0."""
    if fraction <= 0 or gap_now is None or gap_peak is None or not gap_peak > 0:
        return False
    return gap_now <= (1.0 - fraction) * gap_peak


def evaluate(*, side: str, entry: float, initial_stop: float, opened_ms: int, closed: list, fast_period: int, slow_period: int, peak: float | None, rules: ReversalRules) -> str | None:
    """The reversal exit reason the latest CLOSED candle gives, or None. `closed` is the strategy's closed candles (oldest first, enough history for the EMAs); only candles that started at or after the entry count
    for the gap peak. `peak` is the best price since entry (trades.peak_price)."""
    if not rules.any_on or not closed:
        return None
    closes = [c.close for c in closed]
    fast, slow = ema_series(closes, fast_period), ema_series(closes, slow_period)
    last = len(closed) - 1
    if closed[last].timestamp < opened_ms:
        return None  # no candle has closed after the entry yet
    if rules.fast_cross and fast_cross_hit(side, close=closes[last], fast=fast[last], peak=peak, entry=entry, initial_stop=initial_stop, min_profit_r=rules.min_profit_r):
        return REASON_FAST
    if rules.gap_fraction > 0:
        sign = 1.0 if side == "long" else -1.0
        gaps = [sign * (fast[i] - slow[i]) for i in range(len(closed)) if closed[i].timestamp >= opened_ms and fast[i] is not None and slow[i] is not None]
        if gaps and gap_shrink_hit(side, gap_now=gaps[-1], gap_peak=max(gaps), fraction=rules.gap_fraction):
            return REASON_GAP
    return None
