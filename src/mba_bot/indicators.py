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


def rsi(closes: list[float], period: int = 14) -> list[float | None]:
    """Relative Strength Index with Wilder's smoothing.

    Same length/None-padding convention as `ema`. The first value sits at index `period` (it needs `period` price changes). A window with
    no losses is 100, a window with no gains is 0, and a flat window (no change at all) is 50.
    """
    if period <= 0:
        raise ValueError("period must be positive")
    n = len(closes)
    if n < period + 1:
        return [None] * n
    gains = [max(closes[i] - closes[i - 1], 0.0) for i in range(1, n)]
    losses = [max(closes[i - 1] - closes[i], 0.0) for i in range(1, n)]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def value(g: float, l: float) -> float:
        if g == 0.0 and l == 0.0:
            return 50.0
        if l == 0.0:
            return 100.0
        return 100.0 - 100.0 / (1.0 + g / l)

    result: list[float | None] = [None] * period
    result.append(value(avg_gain, avg_loss))
    for i in range(period, n - 1):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
        result.append(value(avg_gain, avg_loss))
    return result


def adx(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> list[float | None]:
    """Average Directional Index (Wilder). 0 to 100: low means a range, high means a trend (it says nothing about direction).

    Same length/None-padding convention as `ema`. The first value sits at index `2 * period - 1`: `period` bars to seed the smoothed
    directional movement and true range, then `period` directional-index values averaged to seed the ADX.
    """
    if not (len(highs) == len(lows) == len(closes)):
        raise ValueError("highs, lows, closes must be the same length")
    if period <= 0:
        raise ValueError("period must be positive")
    n = len(closes)
    if n < 2 * period:
        return [None] * n

    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    tr = [0.0] * n
    for i in range(1, n):
        up, down = highs[i] - highs[i - 1], lows[i - 1] - lows[i]
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0
        tr[i] = true_range(highs[i], lows[i], closes[i - 1])

    s_tr = sum(tr[1 : period + 1])
    s_plus = sum(plus_dm[1 : period + 1])
    s_minus = sum(minus_dm[1 : period + 1])

    def dx(p: float, m: float, t: float) -> float:
        if t <= 0:
            return 0.0
        pdi, mdi = 100.0 * p / t, 100.0 * m / t
        total = pdi + mdi
        return 0.0 if total == 0 else 100.0 * abs(pdi - mdi) / total

    dxs = [dx(s_plus, s_minus, s_tr)]
    for i in range(period + 1, n):
        s_tr = s_tr - s_tr / period + tr[i]
        s_plus = s_plus - s_plus / period + plus_dm[i]
        s_minus = s_minus - s_minus / period + minus_dm[i]
        dxs.append(dx(s_plus, s_minus, s_tr))

    result: list[float | None] = [None] * (2 * period - 1)
    value = sum(dxs[:period]) / period
    result.append(value)
    for d in dxs[period:]:
        value = (value * (period - 1) + d) / period
        result.append(value)
    return result
