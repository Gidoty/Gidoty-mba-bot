"""Profit protection shared by every strategy (docs/safety/profit_peak_tracking.md and the parts that follow). Pure: no I/O, no clock, no database.

Part A (this file so far): the best price reached since the entry, also called the peak or the maximum favourable excursion. For a long it is the highest price seen since the entry, for a short the lowest. It only
ever moves in the favourable direction. Nothing here is read when deciding an entry."""
from __future__ import annotations


def is_better(side: str, a: float, b: float) -> bool:
    """True when price `a` is strictly more favourable than `b` for this side: higher for a long, lower for a short."""
    return a > b if side == "long" else a < b


def advance_peak(side: str, previous: float | None, candidate: float | None) -> float | None:
    """The peak after seeing `candidate`: never worse than `previous`. A missing or non-positive candidate changes nothing."""
    if candidate is None or not candidate > 0:
        return previous
    if previous is None:
        return candidate
    return candidate if is_better(side, candidate, previous) else previous


def peak_from_candles(side: str, entry: float, opened_ms: int, candles: list, price: float | None, previous: float | None) -> float:
    """The peak so far: the stored `previous` (or the entry price when none is stored), then the highest high (long) or lowest low (short) of every candle that STARTED at or after the entry, then the live `price`.
    A candle that started before the entry is skipped: its extreme may have happened before the trade existed. The result is never worse than the entry price."""
    best = advance_peak(side, previous, entry) if previous is not None else entry
    for c in candles:
        if c.timestamp < opened_ms:
            continue
        best = advance_peak(side, best, c.high if side == "long" else c.low)
    return advance_peak(side, best, price)
