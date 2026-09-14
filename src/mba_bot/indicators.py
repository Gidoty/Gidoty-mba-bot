"""Pure technical-indicator math: EMA and ATR.

No exchange, no I/O - plain lists in, plain lists out - so this is cheap to
unit test and reuse from both live evaluation and backtesting later.
"""
from __future__ import annotations


def ema(values: list[float], period: int) -> list[float | None]:
    """Exponential moving average.

    Returns a list the same length as `values`; entries before the first
    full `period` window are `None` since EMA isn't defined yet (matches
    how most charting libraries seed EMA off a simple-average warm-up).
    """
    if period <= 0:
        raise ValueError("period must be positive")
    if len(values) < period:
        return [None] * len(values)

    result: list[float | None] = [None] * (period - 1)
    multiplier = 2 / (period + 1)

    seed = sum(values[:period]) / period
    result.append(seed)
    prev = seed
    for value in values[period:]:
        current = (value - prev) * multiplier + prev
        result.append(current)
        prev = current
    return result


def true_range(high: float, low: float, prev_close: float | None) -> float:
    if prev_close is None:
        return high - low
    return max(high - low, abs(high - prev_close), abs(low - prev_close))


def atr(highs: list[float], lows: list[float], closes: list[float], period: int) -> list[float | None]:
    """Average True Range using Wilder's smoothing.

    Same length/None-padding convention as `ema` above.
    """
    if not (len(highs) == len(lows) == len(closes)):
        raise ValueError("highs, lows, closes must be the same length")
    if period <= 0:
        raise ValueError("period must be positive")
    n = len(closes)
    if n < period + 1:
        return [None] * n

    trs: list[float] = []
    for i in range(n):
        prev_close = closes[i - 1] if i > 0 else None
        trs.append(true_range(highs[i], lows[i], prev_close))

    result: list[float | None] = [None] * period
    seed = sum(trs[1 : period + 1]) / period
    result.append(seed)
    prev = seed
    for i in range(period + 1, n):
        current = (prev * (period - 1) + trs[i]) / period
        result.append(current)
        prev = current
    return result
