"""Market features measured at a decision point, for RESEARCH only.

Nothing in the trading path reads these values. They are recorded next to
each trade and each logged decision (telemetry.py) and recomputed by the
research tooling (research/), so a later phase can ask "which conditions
did winners and losers share?" with data instead of opinion.

Pure functions over the same candle window the strategy sees (the last
`OHLCV_LIMIT` candles, the final one possibly still forming). Every value is
either a finite float or None; nothing here raises on short or odd input.
"""
from __future__ import annotations

import math
from statistics import pstdev

from .indicators import atr, ema
from .strategy import Candle

FEATURE_VERSION = 1


def _finite(value: float | None, digits: int = 8) -> float | None:
    if value is None:
        return None
    try:
        if not math.isfinite(value):
            return None
    except TypeError:
        return None
    return round(float(value), digits)


def adx(highs: list[float], lows: list[float], closes: list[float], period: int = 14) -> list[float | None]:
    """Wilder's Average Directional Index, same None-padding convention as
    `indicators.ema`/`atr`."""
    n = len(closes)
    if period <= 0 or n < 2 * period + 1:
        return [None] * n
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    tr = [0.0] * n
    for i in range(1, n):
        up = highs[i] - highs[i - 1]
        down = lows[i - 1] - lows[i]
        plus_dm[i] = up if (up > down and up > 0) else 0.0
        minus_dm[i] = down if (down > up and down > 0) else 0.0
        tr[i] = max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
    sm_tr = sum(tr[1 : period + 1])
    sm_p = sum(plus_dm[1 : period + 1])
    sm_m = sum(minus_dm[1 : period + 1])
    dx: list[float | None] = [None] * n

    def _dx(s_tr: float, s_p: float, s_m: float) -> float:
        if s_tr <= 0:
            return 0.0
        pdi = 100.0 * s_p / s_tr
        mdi = 100.0 * s_m / s_tr
        denom = pdi + mdi
        return 0.0 if denom == 0 else 100.0 * abs(pdi - mdi) / denom

    dx[period] = _dx(sm_tr, sm_p, sm_m)
    for i in range(period + 1, n):
        sm_tr = sm_tr - sm_tr / period + tr[i]
        sm_p = sm_p - sm_p / period + plus_dm[i]
        sm_m = sm_m - sm_m / period + minus_dm[i]
        dx[i] = _dx(sm_tr, sm_p, sm_m)
    out: list[float | None] = [None] * n
    first = period * 2
    seed_vals = [d for d in dx[period:first] if d is not None]
    if len(seed_vals) < period:
        return out
    prev = sum(seed_vals) / period
    out[first - 1] = prev
    for i in range(first, n):
        prev = (prev * (period - 1) + (dx[i] or 0.0)) / period
        out[i] = prev
    return out


def efficiency_ratio(closes: list[float], period: int = 20) -> float | None:
    """Kaufman's efficiency ratio over the last `period` bars: net move divided
    by the sum of absolute bar-to-bar moves. 1 = a straight line, 0 = pure chop."""
    if period <= 0 or len(closes) < period + 1:
        return None
    window = closes[-(period + 1) :]
    path = sum(abs(window[i] - window[i - 1]) for i in range(1, len(window)))
    if path <= 0:
        return 0.0
    return abs(window[-1] - window[0]) / path


def _ret(closes: list[float], bars: int) -> float | None:
    if len(closes) <= bars or closes[-1 - bars] <= 0:
        return None
    return closes[-1] / closes[-1 - bars] - 1.0


def _percentile_rank(values: list[float], x: float) -> float | None:
    vals = [v for v in values if v is not None]
    if len(vals) < 5:
        return None
    return sum(1 for v in vals if v <= x) / len(vals)


def compute_features(
    candles: list[Candle],
    *,
    ema_fast_period: int,
    ema_slow_period: int,
    atr_period: int,
    atr_rolling_window: int,
    bar_seconds: int = 3600,
    now_ms: int | None = None,
) -> dict[str, float | None]:
    """Features at the LAST candle of `candles`. `now_ms`, when given, adds how
    far through that candle we are (a value below 1 means the last candle is
    still forming, i.e. the number can still change before it closes)."""
    out: dict[str, float | None] = {"feature_version": FEATURE_VERSION}
    if len(candles) < 3:
        return out
    closes = [c.close for c in candles]
    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    vols = [c.volume for c in candles]
    last = closes[-1]

    fast = ema(closes, ema_fast_period)
    slow = ema(closes, ema_slow_period)
    atrs = atr(highs, lows, closes, atr_period)
    a = atrs[-1]
    out["close"] = _finite(last)
    out["atr"] = _finite(a)
    if a and last:
        atr_pct = a / last
        out["atr_pct"] = _finite(atr_pct)
        window = [(x / c) for x, c in zip(atrs[-atr_rolling_window:], closes[-atr_rolling_window:]) if x is not None and c]
        if window:
            avg = sum(window) / len(window)
            out["atr_pct_rolling_avg"] = _finite(avg)
            out["atr_ratio"] = _finite(atr_pct / avg) if avg > 0 else None
            out["atr_pct_percentile"] = _finite(_percentile_rank(window, atr_pct))
    if fast[-1] is not None and slow[-1] is not None and a:
        out["ema_fast"] = _finite(fast[-1])
        out["ema_slow"] = _finite(slow[-1])
        out["ema_gap_atr"] = _finite((fast[-1] - slow[-1]) / a)
        out["close_vs_slow_atr"] = _finite((last - slow[-1]) / a)
        if len(slow) > 6 and slow[-6] is not None:
            out["slow_slope_atr_per_bar"] = _finite((slow[-1] - slow[-6]) / 5.0 / a)
    adx_vals = adx(highs, lows, closes, 14)
    out["adx14"] = _finite(adx_vals[-1])
    out["efficiency_ratio20"] = _finite(efficiency_ratio(closes, 20))
    for label, bars in (("ret_6", 6), ("ret_24", 24), ("ret_72", 72), ("ret_168", 168)):
        out[label] = _finite(_ret(closes, bars))
    if len(closes) > 25:
        rets = [math.log(closes[i] / closes[i - 1]) for i in range(len(closes) - 24, len(closes)) if closes[i - 1] > 0 and closes[i] > 0]
        if len(rets) >= 12:
            out["realized_vol_24"] = _finite(pstdev(rets) * math.sqrt(24))
    if len(vols) > 21:
        base = sum(vols[-21:-1]) / 20.0
        if base > 0:
            out["last_bar_volume_ratio"] = _finite(vols[-1] / base)
            out["prev_bar_volume_ratio"] = _finite(vols[-2] / base)
    rng = highs[-1] - lows[-1]
    out["last_bar_range_atr"] = _finite(rng / a) if a else None
    if a and rng > 0:
        out["last_bar_close_location"] = _finite((last - lows[-1]) / rng)
    if now_ms is not None:
        started = candles[-1].timestamp
        progress = (now_ms - started) / (bar_seconds * 1000.0)
        out["last_bar_progress"] = _finite(max(0.0, min(progress, 1.0)), 4)
    return out
